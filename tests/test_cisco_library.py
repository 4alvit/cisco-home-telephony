"""Music selection uses filenames and metadata, never cloud file contents."""
import importlib.util
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import quote


SPEC = importlib.util.spec_from_file_location(
    'cisco_library', Path(__file__).resolve().parents[1] / 'scripts/cisco_library.py')
library = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(library)


class LibraryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / 'library'
        self.root.mkdir()
        self.config = {'directory': str(self.root),
                       'media_source_prefix': 'media-source://media_source/music/Example'}

    def add(self, relative, data=b'audio'):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def selected(self, query, config=None):
        return library.select_tracks(config or self.config, query)

    def paths(self, query, config=None):
        return [item['relative_path'] for item in self.selected(query, config)]

    def refused(self, query, reason, config=None):
        with self.assertRaises(library.LibraryError) as caught:
            self.selected(query, config)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(str(caught.exception), reason)

    def test_formats_literal_search_and_deterministic_sort(self):
        for name in ('Album/z.FLAC', 'album a.Mp3', 'ALBUM B.mp3', 'Album/notes.txt',
                     'Other/song[1].mp3', 'Other/song2.mp3'):
            self.add(name)
        self.assertEqual(self.paths('aLbUm'), ['album a.Mp3', 'ALBUM B.mp3', 'Album/z.FLAC'])
        self.assertEqual(self.paths('[1]'), ['Other/song[1].mp3'])
        self.assertEqual(self.paths('song*'), ['Other/song2.mp3', 'Other/song[1].mp3'])

    def test_masks_match_names_and_paths_without_treating_regex_as_operators(self):
        for name in ('Album/ПЕСНЯ 1.FLAC', 'Album/Песня 2.flac', 'Other/Песня 3.mp3',
                     'Other/song[1].mp3', 'Other/song2.mp3'):
            self.add(name)
        self.assertEqual(self.paths('песня *.flac'), ['Album/ПЕСНЯ 1.FLAC', 'Album/Песня 2.flac'])
        self.assertEqual(self.paths('album/песня ?.flac'), ['Album/ПЕСНЯ 1.FLAC', 'Album/Песня 2.flac'])
        self.assertEqual(self.paths(str(self.root / 'Album' / '*.FLAC')),
                         ['Album/ПЕСНЯ 1.FLAC', 'Album/Песня 2.flac'])
        self.assertEqual(self.paths('*song[1]*'), ['Other/song[1].mp3'])
        self.assertEqual(self.paths('song?.mp3'), ['Other/song2.mp3'])
        self.refused('(song|песня)', 'library_no_matches')

    def test_exact_path_with_mask_characters_wins_and_mask_limit_is_not_truncated(self):
        self.add('literal?.mp3')
        self.add('literal1.mp3')
        self.assertEqual(self.paths('literal?.mp3'), ['literal?.mp3'])
        self.refused('literal*', 'library_too_many_matches', dict(self.config, max_matches=1))

    def test_masks_cannot_escape_root_or_follow_links(self):
        track = self.add('track.mp3')
        (self.root / 'alias.mp3').symlink_to(track)
        self.refused('ali*.mp3', 'library_symlink_not_allowed')
        self.refused('../*.mp3', 'library_path_escape')
        self.refused(str(self.parent / '*.mp3'), 'library_path_escape')

    def test_exact_file_directory_and_absolute_paths(self):
        track = self.add('Album/first.mp3')
        self.add('Album/second.flac')
        self.add('Album extra/third.mp3')
        self.assertEqual(self.paths('Album/first.mp3'), ['Album/first.mp3'])
        self.assertEqual(self.paths(str(track)), ['Album/first.mp3'])
        self.assertEqual(self.paths('Album'), ['Album/first.mp3', 'Album/second.flac'])
        self.assertEqual(len(self.paths('.')), 3)
        self.refused('Missing/file.mp3', 'library_no_matches')

    def test_configured_root_symlink_and_tilde_are_supported(self):
        self.add('track.mp3')
        alias = self.parent / 'music-link'
        alias.symlink_to(self.root, target_is_directory=True)
        config = dict(self.config, directory=str(alias))
        self.assertEqual(self.paths(str(alias / 'track.mp3'), config), ['track.mp3'])
        self.assertEqual(self.paths(str(self.root / 'track.mp3'), config), ['track.mp3'])
        with patch.dict(os.environ, HOME=str(self.parent)):
            config['directory'] = '~/music-link'
            self.assertEqual(self.paths('~/music-link/track.mp3', config), ['track.mp3'])

    def test_traversal_outside_paths_and_child_symlinks_are_refused(self):
        track = self.add('track.mp3')
        outside = self.parent / 'outside'
        outside.mkdir()
        (outside / 'foreign.mp3').write_bytes(b'foreign')
        (self.root / 'escape').symlink_to(outside, target_is_directory=True)
        (self.root / 'alias.mp3').symlink_to(track)
        for query in ('../outside/foreign.mp3', 'Album/../../foreign.mp3',
                      str(outside / 'foreign.mp3'), r'..\outside\foreign.mp3'):
            self.refused(query, 'library_path_escape')
        for query in ('escape', 'escape/foreign.mp3', 'alias.mp3'):
            self.refused(query, 'library_symlink_not_allowed')
        self.assertEqual(self.paths('.'), ['track.mp3'])

    def test_special_file_never_gets_opened(self):
        os.mkfifo(self.root / 'pipe.mp3')
        self.refused('pipe.mp3', 'library_special_file')

    def test_unicode_percent_and_fragment_names_are_encoded_without_normalizing(self):
        name = 'Album ü/Игра #1 100% cafe\u0301.FLAC'
        self.add(name, b'1234567')
        track = self.selected(name)[0]
        self.assertEqual(track, {'relative_path': name, 'size_bytes': 7,
                                'media_content_id': self.config['media_source_prefix'] + '/' + quote(name, safe='/')})
        self.assertIn('%23', track['media_content_id'])
        self.assertIn('%25', track['media_content_id'])
        self.assertIn('e%CC%81', track['media_content_id'])

    def test_dataless_large_file_is_selected_without_opening_content(self):
        calls = []
        class Entry:
            name = 'cloud.flac'
            def stat(self, *, follow_symlinks):
                calls.append(follow_symlinks)
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_size=2**31,
                                       st_blocks=0, st_flags=getattr(stat, 'UF_DATALESS', 0x40000000))
        class Scan:
            def __enter__(self): return iter([Entry()])
            def __exit__(self, *_): pass
        original_open = os.open
        def directory_only(path, flags, *args, **kwargs):
            self.assertTrue(flags & os.O_DIRECTORY)
            self.assertTrue(flags & os.O_NOFOLLOW)
            return original_open(path, flags, *args, **kwargs)
        with patch.object(library.os, 'scandir', return_value=Scan()), \
             patch.object(library.os, 'open', side_effect=directory_only), \
             patch('builtins.open', side_effect=AssertionError('File content must not be opened')), \
             patch.object(Path, 'open', side_effect=AssertionError('File content must not be opened')):
            track = self.selected('cloud')[0]
        self.assertEqual(track['size_bytes'], 2**31)
        self.assertEqual(calls, [False])

    def test_depth_four_is_included_but_depth_five_is_not_scanned(self):
        self.add('a/b/c/d/track.mp3')
        self.add('a/b/c/d/e/track.mp3')
        self.assertEqual(self.paths('track'), ['a/b/c/d/track.mp3'])
        self.refused('a/b/c/d/e/track.mp3', 'library_depth_limit')

    def test_match_limit_and_scan_limit_return_no_partial_selection(self):
        for index in range(3):
            self.add(f'track{index}.mp3')
        self.refused('track', 'library_too_many_matches', dict(self.config, max_matches=2))
        self.assertEqual(len(self.paths('track', dict(self.config, max_matches=3))), 3)
        with patch.object(library, 'MAX_SCAN_ENTRIES', 2):
            self.refused('track0.mp3', 'library_scan_limit')

    def test_directory_replacement_with_symlink_is_not_followed(self):
        self.add('Album/track.mp3')
        outside = self.parent / 'outside'
        outside.mkdir()
        (outside / 'foreign.mp3').write_bytes(b'foreign')
        original_open = os.open
        def replace(path, flags, *args, **kwargs):
            if path == 'Album':
                (self.root / 'Album').rename(self.parent / 'original')
                (self.root / 'Album').symlink_to(outside, target_is_directory=True)
            return original_open(path, flags, *args, **kwargs)
        with patch.object(library.os, 'open', side_effect=replace):
            self.refused('Album', 'library_unavailable')

    def test_bad_config_and_queries_have_only_fixed_public_reasons(self):
        invalid = [None, {}, dict(self.config, extra=True), dict(self.config, max_matches=True),
                   dict(self.config, max_matches=0), dict(self.config, max_matches=51),
                   dict(self.config, directory='relative')]
        for prefix in ('https://example.test/music', 'media-source://other/music',
                       'media-source://media_source/music/../private',
                       'media-source://media_source/music/%2e%2e/private',
                       'media-source://media_source/music/a%2fb',
                       'media-source://media_source/music/%FF',
                       'media-source://media_source/music?token=private'):
            invalid.append(dict(self.config, media_source_prefix=prefix))
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(library.LibraryError) as caught:
                library.validate_config(config)
            self.assertEqual(str(caught.exception), 'invalid_music_config')
        for query in ('', ' ', '\x00', '\n', None):
            self.refused(query, 'invalid_library_query')

    def test_absent_library_is_not_opened_during_config_validation(self):
        config = dict(self.config, directory=str(self.parent / 'absent'))
        self.assertEqual(library.validate_config(config)['max_matches'], 50)
        self.refused('track', 'library_unavailable', config)


if __name__ == '__main__':
    unittest.main()
