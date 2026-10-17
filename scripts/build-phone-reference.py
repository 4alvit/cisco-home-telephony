#!/usr/bin/env python3
"""Build the English, three-page US Letter phone reference. Requires reportlab."""
import argparse
import json
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, white
from reportlab.lib.utils import simpleSplit

ROOT = Path(__file__).resolve().parents[1]
INK = HexColor('#152C43')
TEAL = HexColor('#087E8B')
BLUE = HexColor('#2459A6')
PALE = HexColor('#EDF5F7')
GRAY = HexColor('#526575')


def paragraph(c, value, x, y, width, size=10, leading=14, color=INK, font='Helvetica'):
    c.setFillColor(color); c.setFont(font, size)
    lines = simpleSplit(value, font, size, width)
    for line in lines:
        c.drawString(x, y, line); y -= leading
    return y


def title(c, kicker, heading, subtitle):
    c.setFillColor(INK); c.rect(0, 672, 612, 120, fill=1, stroke=0)
    c.setFillColor(HexColor('#80D9D0')); c.setFont('Helvetica-Bold', 10)
    c.drawString(42, 755, kicker)
    c.setFillColor(white); c.setFont('Helvetica-Bold', 28); c.drawString(42, 717, heading)
    paragraph(c, subtitle, 42, 692, 528, 10, 13, white)


def footer(c, page):
    c.setStrokeColor(HexColor('#D7E1E7')); c.line(42, 44, 570, 44)
    paragraph(c, 'Cisco 7945G  |  Home telephony  |  English reference  |  17 October 2026', 42, 29, 490, 8, 10, GRAY)
    c.setFont('Helvetica-Bold', 9); c.drawRightString(570, 29, str(page))


def block(c, heading, text, y, color=TEAL):
    c.setFillColor(color); c.setFont('Helvetica-Bold', 13); c.drawString(42, y, heading)
    return paragraph(c, text, 42, y-21, 526, 10, 14) - 22


def table(c, rows, y, row_height=38):
    c.setFillColor(INK); c.roundRect(42,y-8,528,26,4,fill=1,stroke=0)
    c.setFillColor(white); c.setFont('Helvetica-Bold',9)
    for x,label in [(54,'DIAL'),(107,'SERVICE'),(306,'WHAT TO EXPECT')]: c.drawString(x,y,label)
    y -= 36
    for i,(number,name,description) in enumerate(rows):
        if i % 2 == 0:
            c.setFillColor(PALE); c.rect(42,y-row_height+13,528,row_height,fill=1,stroke=0)
        c.setFillColor(BLUE); c.setFont('Helvetica-Bold',14); c.drawString(54,y-5,number)
        paragraph(c,name,107,y-2,191,10,12,font='Helvetica-Bold')
        paragraph(c,description,306,y-2,254,9,11)
        y -= row_height
    return y


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='output/pdf/cisco-phone-reference.pdf')
    args = parser.parse_args()
    target = Path(args.output); target.parent.mkdir(parents=True, exist_ok=True)
    stations = json.loads((ROOT/'containers/asterisk/radio-catalog.json').read_text())['stations']
    c = canvas.Canvas(str(target), pagesize=(612,792), pageCompression=1)
    c.setTitle('Cisco home phones - dial guide and Home Assistant controls')
    c.setAuthor('Cisco Home Telephony')
    title(c, 'KEEP BESIDE YOUR PHONE', 'Dial guide', 'Lift the handset or press Speaker, dial a number, then end the call when finished.')
    y=655
    c.setFont('Helvetica-Bold',13);c.setFillColor(TEAL);c.drawString(42,y,'Ten phone identities, two independent lines each')
    for i,(a,b) in enumerate([(201,202),(203,204),(205,206),(207,208),(210,211),(212,213),(214,215),(216,217),(218,219),(220,221)]):
        x=42+(i%5)*106; card_y=593-(i//5)*60
        c.setFillColor(PALE);c.roundRect(x,card_y,98,48,6,fill=1,stroke=0)
        c.setFillColor(GRAY);c.setFont('Helvetica',9);c.drawString(x+9,card_y+32,f'PHONE {i+1}')
        c.setFillColor(INK);c.setFont('Helvetica-Bold',14);c.drawString(x+9,card_y+10,f'{a} / {b}')
    paragraph(c, 'Unplugged phones keep their numbers. Online status depends on which devices are connected.',42,490,528,8,10,GRAY)
    rows=[('600','Echo test','Hear your voice returned to you.'),('601','Test tone','Check that audio plays.'),
          ('620','Ring all phones','Calls each phone\'s primary line; DND phones are skipped.'),
          ('630','Home Assistant','Russian voice assistant using Silero Baya.'),
          ('640','Manual tone test','Finite test tone; not the timer/alarm ringer.')]
    table(c,rows,462,32)
    y=block(c,'Mac SIP client - 209', 'Use the dedicated home SIP account over TCP. Dial internal numbers and enabled services in the app. It does not join ring group 620. Keep the Mac awake and the app running to receive calls. See the setup guide for the current call-validation result.',240,BLUE)
    y=block(c,'Choose a line and end the call', 'Use either numbered line button on the Cisco phone. Return the handset to its cradle or end the active speaker call when finished. A dark screen can be woken with Display (two overlapping rectangles).',y)
    paragraph(c,'Radio station numbers: page 2. Home Assistant calls, timers, alarms and screen messages: page 3.',42,91,528,10,14,GRAY)
    footer(c,1);c.showPage()
    title(c, 'RADIO ON DEMAND', 'Music & radio', 'Dial a station, listen, then hang up. Streams run only while a call is active.')
    rows=[(v['extension'],v['name'],v['genre']) for v in sorted(stations.values(),key=lambda v:int(v['extension']))]
    table(c,rows,631,38)
    y=block(c,'Children\'s channels 708-710', '708: toddler music. 709: lullabies. 710: children\'s songs. For these new numbers, leave the handset down and speaker/headset off. Type the full number, then press Dial; do not press a line key first. All three are deployed. A listener confirmed music on 708.',177,BLUE)
    paragraph(c,'Radio needs Internet access. The default call limit is 30 minutes; your administrator can set up to 2 hours. Hang up to stop immediately. An unavailable stream gives a short local tone and ends the call. Echo online is the current online service, not the former Moscow FM broadcast.',42,91,528,9,12,GRAY)
    footer(c,2);c.showPage()
    title(c, 'CONTROL FROM HOME ASSISTANT', 'Calls, alarms & messages', 'Updated alerts require the matching gateway and Home Assistant integration.')
    y=640
    y=block(c,'Set an alarm for selected days', 'Choose the local time and weekdays, then enable the alarm. It keeps ringing until you answer or reject it on the phone, press Stop alert, or disable Alarm enabled. There is no auto-answer or audio after answering.',y)
    y=block(c,'Start or stop a timer', 'Set the duration and press Start timer. At zero, the phone rings until you answer, reject it or press Stop alert. Cancel timer cancels the countdown and stops its active alert. Busy, offline or DND phones can miss alerts; recovery does not replay them.',y)
    y=block(c,'Call controls', 'Choose the line and allowed destination, then use Dial. HA Answer handles normal ringing calls, not timer/alarm alerts. Hang up ends the selected active call. Speaker toggles a physical key; HA cannot reliably report the hardware speaker or handset state.',y)
    y=block(c,'Do Not Disturb and auto-answer', 'DND blocks local calls routed through the PBX and gateway calls. It is separate from the phone\'s native DND icon. Optional auto-answer is off by default and applies only to normal calls. Timers and alarms always require manual dismissal.',y)
    y=block(c,'Send text, photos or still sprites', 'Send a text page or an approved local PNG/JPEG image through the integration. Pictures fit the 298 x 156 Services pane. Press Exit on the phone to return. These are local screen notifications, not mobile SMS/MMS; animation and video are not supported.',y)
    y=block(c,'External calls and free SIP accounts', '801-803 are reserved for optional provider tests and work only after both the account and its call route are verified. Free SIP-to-SIP calling does not mean free calls to ordinary telephone numbers. Google Voice has no supported free direct Asterisk trunk. Use another working service for emergency calls.',y,BLUE)
    y=block(c,'Voice assistant 630', "Use the phone's primary line. After discovery, enable Allow calls and choose its Russian Baya pipeline in HA. Do this once per phone; generic controls do not complete voice setup.",y,BLUE)
    paragraph(c,'Manual timer dismissal and HA Stop passed. A scheduled alarm rang beyond 90 seconds. Setup: github.com/4alvit/cisco-home-telephony',42,84,528,9,12,GRAY)
    footer(c,3);c.save()
    print(target)


if __name__=='__main__':main()
