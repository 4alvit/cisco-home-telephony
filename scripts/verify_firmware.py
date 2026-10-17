#!/usr/bin/env python3
"""Verify a locally supplied firmware set against an operator-reviewed manifest.

This verifies integrity and completeness, not Cisco authenticity or licensing.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


def verify(directory, manifest):
    root=Path(directory).resolve()
    data=json.loads(Path(manifest).read_text())
    if data.get('model')!='CP-7945G' or data.get('reviewed') is not True:
        raise ValueError('reviewed CP-7945G manifest required')
    load=data.get('load_id','')
    if not re.fullmatch(r'SIP45\.[A-Za-z0-9-]+',load):
        raise ValueError('invalid SIP45 load_id (omit .loads)')
    entries=data['files']
    if not isinstance(entries,dict) or load+'.loads' not in entries:
        raise ValueError('manifest must include the exact .loads file and all package files')
    if not any(n.endswith('.sbn') for n in entries):
        raise ValueError('manifest has no component .sbn files')
    for name,digest in entries.items():
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',name) or name in ('.','..'):
            raise ValueError('flat filenames only')
        if not re.fullmatch(r'[a-fA-F0-9]{64}',digest):
            raise ValueError('every file needs a reviewed SHA-256')
        file=root/name
        if file.is_symlink() or not file.is_file() or file.resolve().parent!=root:
            raise ValueError(f'missing, unsafe or nonregular firmware file: {name}')
        if hashlib.sha256(file.read_bytes()).hexdigest()!=digest.lower():
            raise ValueError(f'checksum mismatch: {name}')
    actual={p.name for p in root.iterdir()}
    if actual!=set(entries):
        raise ValueError('firmware directory contains files absent from the manifest; stage only reviewed files')
    return len(entries),load


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory',default='.local/firmware')
    p.add_argument('--manifest',default='.local/firmware-manifest.json')
    a=p.parse_args()
    try:
        n,load=verify(a.directory,a.manifest)
    except (ValueError,OSError,KeyError,TypeError) as e:
        sys.exit(f'Firmware verification stopped: {e}')
    print(f'Integrity verified: {n} files, {load}. Confirm licensing, provenance and hardware upgrade path separately.')
