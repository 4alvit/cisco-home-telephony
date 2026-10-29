# Dial-in time and Home Assistant music

`dial_audio.py` answers a local call with either a current-time announcement or a configured audio file from the Home Assistant media library. The caller hears the audio in the existing call; a second phone call is not required. It works with ordinary SIP clients as well as Cisco phones.

The example uses **555** for local time and **777** for one configured song. The song restarts at the beginning on every call and ends normally or when the caller hangs up. Select another number if either extension is already in use.

To originate these calls from `cisco 222 555` on a LifeSize Phone, configure the
[optional native CLI driver](cli-lifesize.md). Asterisk registration by itself
does not enable remote control of a non-Cisco phone.

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

Add exact `555` and `777` entries with `Timeout="0"` to the existing TFTP
`dialplan.xml`. Preserve all other patterns. Then refresh/restart each online
Cisco phone and verify a real call to the complete service number. Updating the
TFTP server alone does not update a phone's currently loaded dial template.
Leave the updated profiles available for offline phones to fetch when powered on.
Ordinary SIP clients such as LifeSize do not use this Cisco XML file.

### Accepted CLI request followed by fast busy

`cisco LINE 555` returning `accepted` means that the phone acknowledged the
remote dial command; it does not confirm the number that reached Asterisk.
On the tested 7945G, a stale dial template with no `5` prefix caused `Dial:555`
to send **5** immediately. Asterisk correctly rejected that incomplete number,
producing fast busy. The same risk applies to other newly added number patterns.
The Dial softkey is not a reliable workaround for this remote-dial case.

Check the Asterisk call log for the actual extension (`5@home` versus
`555@home`). If truncated, check the served `dialplan.xml`, refresh the affected
phone's configuration, and repeat one call. Do not add a PBX alias for the first
digit: that hides the stale profile and can route other numbers incorrectly.
For an idle, configured 7945G, the normal restart is **Settings**, then
`* * # * *`. This preserves the configuration; a factory reset is unnecessary.
Wait for registration before retrying. With authenticated remote keypad access,
first verify the Settings screen and idle call state, then send that sequence
once. An ambiguous last-key response may mean the reboot already began; inspect
reachability and registration rather than repeating it.

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
