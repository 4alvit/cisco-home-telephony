#!/usr/bin/env python3
"""Build the English, two-page US Letter phone reference. Requires reportlab."""
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
    y=643
    c.setFont('Helvetica-Bold',13);c.setFillColor(TEAL);c.drawString(42,y,'Four phones, two independent lines each')
    for i,(a,b) in enumerate([(201,202),(203,204),(205,206),(207,208)]):
        x=42+i*135
        c.setFillColor(PALE);c.roundRect(x,570,123,53,8,fill=1,stroke=0)
        c.setFillColor(GRAY);c.setFont('Helvetica',9);c.drawString(x+12,605,f'PHONE {i+1}')
        c.setFillColor(INK);c.setFont('Helvetica-Bold',18);c.drawString(x+12,581,f'{a} / {b}')
    rows=[('600','Echo test','Hear your voice returned to you.'),('601','Test tone','Check that audio plays.'),
          ('620','Ring all phones','Calls each phone\'s primary line; DND phones are skipped.'),
          ('630','Home Assistant','Russian voice assistant using Silero Baya.'),
          ('640','Alarm sound','Short alarm tone; the call ends automatically.')]
    rows += [(v['extension'],v['name'],v['genre']) for v in stations.values()]
    y=543
    c.setFillColor(INK);c.roundRect(42,y-8,528,26,4,fill=1,stroke=0)
    c.setFillColor(white);c.setFont('Helvetica-Bold',9)
    for x,label in [(54,'DIAL'),(107,'SERVICE'),(306,'WHAT TO EXPECT')]:c.drawString(x,y,label)
    y-=34
    for i,(number,name,description) in enumerate(rows):
        if i%2==0:c.setFillColor(PALE);c.rect(42,y-19,528,32,fill=1,stroke=0)
        c.setFillColor(BLUE);c.setFont('Helvetica-Bold',14);c.drawString(54,y-5,number)
        paragraph(c,name,107,y-2,191,9,11,font='Helvetica-Bold')
        paragraph(c,description,306,y-2,254,8.5,10)
        y-=32
    paragraph(c,'Radio needs an Internet connection. Calls are limited to 2 hours; hang up to stop immediately. Echo online is the current online service, not the former Moscow FM broadcast.',42,100,528,9,12,GRAY)
    footer(c,1);c.showPage()
    title(c, 'CONTROL FROM HOME ASSISTANT', 'Calls, alarms & messages', 'Use the Cisco Phone Control device for the phone you want to operate.')
    y=640
    y=block(c,'Set an alarm for selected days', 'Choose an alarm time, enable the desired weekdays, and enable the alarm. Times follow Home Assistant\'s configured local time zone. The selected phone rings; answer to hear the short alarm sound.',y)
    y=block(c,'Start a timer', 'Set the timer duration and press Start timer. Use Cancel timer to stop it. A timer is one-shot. DND, an offline phone, or a busy phone can prevent delivery; missed alerts are not replayed after recovery.',y)
    y=block(c,'Call controls', 'Choose the line and allowed destination, then use Dial. Answer acts on a currently ringing call. Hang up ends the selected active call. Speaker is an explicit button toggle; Home Assistant cannot reliably report the physical speaker or handset state.',y)
    y=block(c,'Do Not Disturb and auto-answer', 'DND blocks local calls routed through the PBX and gateway calls. It is separate from the phone\'s native DND icon. Auto-answer is optional and off by default. When enabled it answers incoming calls; the phone\'s selected audio path may be speaker or headset.',y)
    y=block(c,'Send text, photos or still sprites', 'Send a text page or an approved local PNG/JPEG image through the integration. Pictures fit the 298 x 156 Services pane. Press Exit on the phone to return. These are local screen notifications, not mobile SMS/MMS; animation and video are not supported.',y)
    y=block(c,'External calls and free SIP accounts', '801-803 are reserved for optional provider tests and work only after both the account and its call route are verified. Free SIP-to-SIP calling does not mean free calls to ordinary telephone numbers. Google Voice has no supported free direct Asterisk trunk. Use another working service for emergency calls.',y,BLUE)
    paragraph(c,'If the screen is dark: press the Display button (two overlapping rectangles). For voice assistance: dial 630 and speak after the prompt. Full setup, capabilities and validation: github.com/4alvit/cisco-home-telephony',42,84,528,9,12,GRAY)
    footer(c,2);c.save()
    print(target)


if __name__=='__main__':main()
