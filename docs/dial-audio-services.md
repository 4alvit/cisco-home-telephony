# Dial-in time and Home Assistant music

`dial_audio.py` answers a local call with either a current-time announcement or a configured audio file from the Home Assistant media library. The caller hears the audio in the existing call; a second phone call is not required. It works with ordinary SIP clients as well as Cisco phones.

The example uses **555** for local time and **777** for one configured song. The song restarts at the beginning on every call and ends normally or when the caller hangs up. Select another number if either extension is already in use.

## Configure

Copy [the example](../deploy/dial-audio.example.json) to a private deployment directory and install it as `/etc/asterisk/dial-audio.json`. Keep the real file, media paths, credentials, and household inventory out of the public repository.

- `local_lines` is the complete list of permitted internal extensions. Authorization uses the authenticated PJSIP channel identity, not caller ID.
- `credentials_file` points to JSON containing `url` and `token` for the existing Home Assistant connection. The existing `phone-services.conf` can be reused. An explicit private IPv4 origin is required; credentials are never sent through redirects.
- The clock uses its configured IANA timezone, including daylight-saving changes. Supported announcement languages are `en` and `ru`; select an installed HA TTS engine and one of its advertised voice IDs. The TTS language code must match that engine.
- The media path is a URL-encoded `/media/<configured media directory>/<file>` path served by HA. It is not a NAS filesystem path. MP3 and FLAC work through FFmpeg decoding.
- The media file is fetched through HA with authentication and played in Asterisk. This does not create a Music Assistant queue or a separate HA media-player session.

## Asterisk routes

The image includes the AGI at `/var/lib/asterisk/agi-bin/dial_audio.py`. Add routes to the authenticated internal context, preserving existing routes:

```ini
[home](+)
exten => 555,1,Answer()
 same => n,Set(TIMEOUT(absolute)=90)
 same => n,AGI(dial_audio.py,555)
 same => n,Hangup()

exten => 777,1,Answer()
 same => n,Set(TIMEOUT(absolute)=670)
 same => n,AGI(dial_audio.py,777)
 same => n,Hangup()
```

For an older image, mount the reviewed script from an immutable ConfigMap using one read-only `subPath` with mode `0555`; do not replace the entire AGI directory. Put configuration in the existing private Asterisk Secret. On later image upgrades, remove the script overlay when the image contains the same reviewed implementation. Check both the image and private configuration before removing it.

The worker sets `DIALAUDIOSTATUS=ok` after successful playback. Deployments can initialize it to `failed` and use a short busy tone if it remains unset. HA, TTS, or decoding failure must not result in an outdated time announcement or an unrelated song.

To expose the numbers in the existing Cisco directory and CLI, merge them into the gateway's `dial_allowlist` and `services`, with English labels and kind `automation`. Preserve all existing entries, phone identities, tokens, media settings, and unrelated volume mounts. This is an additive custom route deployment; retain these additions when regenerating configuration.

On deployments with staged voice configuration, the running gateway may read `/run/voice/.local/config/gateway.json` from the `voice-inputs` Secret. Updating only the older `/config` mount will not update the running directory. Inspect the deployment arguments and keep both copies consistent while preserving the other voice-input files.

Add exact `555` and `777` entries with `Timeout="0"` to the existing TFTP `dialplan.xml` for automatic dialing after the third digit. Preserve all other patterns. Cisco phones fetch this file on profile reload or boot; until then, use the **Dial** softkey or the directory entry. Ordinary SIP clients such as LifeSize do not use this Cisco XML file.

## Behavior and checks

Audio is decoded into mono 16 kHz signed PCM and played as `.sln16`. Asterisk can negotiate G.722 with wideband phones and transcode for narrowband endpoints. Source-file stereo and sample rates above 16 kHz do not change the capabilities of the negotiated telephone codec.

The clock reports hours and minutes at preparation time. It rechecks the minute after synthesis and regenerates once if the minute changed. It does not claim a synchronized seconds tick. Each call has a private temporary directory, a bounded duration, and one of four worker slots. Hangup closes that call and cleans its temporary files; the original music file is not modified.

Validate before rollout:

1. Confirm both numbers are unused in the live dialplan and gateway directory.
2. Verify the HA media path and the advertised TTS language/voice without ringing a phone.
3. Save the current deployment and private Secret. Create a newly named immutable Secret and compare-and-swap its deployment reference; do not overwrite an old bootstrap site file.
4. Check `dialplan show 555@home` and `dialplan show 777@home`.
5. Call each number, inspect `pjsip show channelstats`, and verify hangup cleanup. Ask the listener to confirm the actual spoken time and song.

If HA returns HTTP 500 for TTS initialization, inspect its log for unsupported language or voice settings. For a media error, check the HA media-directory mapping and URL encoding first. Keep bearer tokens and signed audio URLs out of diagnostic output.
