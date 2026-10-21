"""Render the disabled-by-default, AMI-only SIP assistant context."""
import re


def render_dialplan(lines):
    """AMI-only private context; caller-facing contexts must not include it."""
    if not lines or any(not re.fullmatch(r'2[0-9]{2}', line) for line in lines):
        raise ValueError('invalid_sip_lines')
    result = ['[cisco-voice-sip]']
    for line in sorted(set(lines)):
        result.extend((f'exten => {line},1,Gosub(cisco-voice-sip-bound,s,1)',
                       f' same => n,GotoIf($["${{DB(cisco-dnd/{line})}}" != "0"]?deny)',
                       f' same => n,Dial(PJSIP/{line},8,ib(cisco-voice-sip-bound^s^1))',
                       ' same => n(deny),Hangup()'))
    result.extend(('exten => _X!,1,Hangup(1)', '', '[cisco-voice-sip-bound]',
        'exten => s,1,GotoIf($[${REGEX("^[0-9a-f]{32}$" ${VOICE_SIP_SESSION})} != 1]?deny)',
        ' same => n,GotoIf($[${REGEX("^[0-9]{10}$" ${VOICE_SIP_END})} != 1]?deny)',
        # EPOCH is truncated to a whole second. Subtract one so this relative
        # timeout cannot extend the absolute deadline by its fractional second.
        ' same => n,Set(VOICE_SIP_REMAIN=$[${VOICE_SIP_END}-${EPOCH}-1])',
        ' same => n,GotoIf($[${VOICE_SIP_REMAIN} <= 0 | ${VOICE_SIP_REMAIN} > 300]?deny)',
        ' same => n,Set(TIMEOUT(absolute)=${VOICE_SIP_REMAIN})',
        ' same => n,Return()', ' same => n(deny),Hangup(21)', ''))
    return '\n'.join(result)
