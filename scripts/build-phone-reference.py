#!/usr/bin/env python3
"""Build the English, US Letter phone reference with a paginated radio catalog. Requires reportlab."""
import argparse
from html import escape
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
    paragraph(c, 'Cisco 7945G  |  Home telephony  |  English reference', 42, 29, 490, 8, 10, GRAY)
    c.setFont('Helvetica-Bold', 9); c.drawRightString(570, 29, str(page))


def block(c, heading, text, y, color=TEAL):
    c.setFillColor(color); c.setFont('Helvetica-Bold', 13); c.drawString(42, y, heading)
    return paragraph(c, text, 42, y-21, 526, 10, 14) - 22


def table(c, rows, y, row_height=38, row_heights=None):
    c.setFillColor(INK); c.roundRect(42,y-8,528,26,4,fill=1,stroke=0)
    c.setFillColor(white); c.setFont('Helvetica-Bold',9)
    for x,label in [(54,'DIAL'),(107,'SERVICE'),(306,'WHAT TO EXPECT')]: c.drawString(x,y,label)
    y -= 36
    for i,(number,name,description) in enumerate(rows):
        height = row_heights[i] if row_heights is not None else row_height
        if i % 2 == 0:
            c.setFillColor(PALE); c.rect(42,y-height+13,528,height,fill=1,stroke=0)
        c.setFillColor(BLUE); c.setFont('Helvetica-Bold',14); c.drawString(54,y-5,number)
        paragraph(c,name,107,y-2,191,10,12,font='Helvetica-Bold')
        paragraph(c,description,306,y-2,254,9,11)
        y -= height
    return y


def radio_pages(stations):
    """Keep every complete radio row above the reserved notes area."""
    pages, rows, heights = [], [], []
    available = 380  # First row baseline 595; next baseline must stay >= 215.
    used = 0
    for station in sorted(stations.values(), key=lambda value: int(value['extension'])):
        row = (station['extension'], station['name'], station['genre'])
        height = max(38,
                     12 * len(simpleSplit(row[1], 'Helvetica-Bold', 10, 191)) + 8,
                     11 * len(simpleSplit(row[2], 'Helvetica', 9, 254)) + 8)
        if height > available:
            raise ValueError(f"Radio entry {row[0]} is too long for a reference page")
        if rows and used + height > available:
            pages.append((rows, heights))
            rows, heights, used = [], [], 0
        rows.append(row); heights.append(height); used += height
    pages.append((rows, heights))
    return pages


def radio_html(stations):
    rows = '\n'.join(
        '<tr><td>' + escape(station['extension']) + '</td><td>'
        + escape(station['name']) + '</td><td>' + escape(station['genre']) + '</td></tr>'
        for station in sorted(stations.values(), key=lambda value: int(value['extension'])))
    return '''<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>Phone radio dial guide</title>
<style>
body{font:16px system-ui;color:#16324a;max-width:950px;margin:35px auto;padding:15px}
h1{color:#087f8c}table{width:100%;border-collapse:collapse}
td,th{text-align:left;padding:10px;border-bottom:1px solid #c9dce5}
th{background:#16324a;color:white}tr:nth-child(even){background:#edf7f8}
td:first-child{font-weight:bold;color:#087f8c}code{background:#edf7f8;padding:4px}
@media print{body{margin:0;font-size:11pt}tr{break-inside:avoid}thead{display:table-header-group}}
</style>
<h1>Phone radio dial guide</h1>
<p>Dial a number, listen, then hang up. CLI: <code>cisco 212 731</code>.
Only stations enabled in your private configuration are available.</p>
<table><thead><tr><th>Number</th><th>Channel</th><th>Music or programme</th></tr></thead>
<tbody>''' + rows + '''</tbody></table>
<p>Artist library playback: <code>cisco 212 play "My Dying Bride"</code> or
<code>cisco 212 play "Darkseed"</code>. Requires matching files in the configured library.</p>
<p>Metal Detector mixes doom with other metal styles. Broadcasters control playlists
and availability. The UVB-76 entry is a third-party shortwave relay, not a music channel.
The white-noise recording has the same shorter per-call limit as the live stations.</p>
<p><a href="radio-and-alarms.md">Configuration and station sources</a> |
<a href="../output/pdf/cisco-phone-reference.pdf">Full printable phone reference</a></p>
</html>
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='output/pdf/cisco-phone-reference.pdf')
    parser.add_argument('--html-output', help='Also regenerate the printable radio HTML from this catalog')
    args = parser.parse_args()
    target = Path(args.output); target.parent.mkdir(parents=True, exist_ok=True)
    stations = json.loads((ROOT/'containers/asterisk/radio-catalog.json').read_text())['stations']
    radio = radio_pages(stations)
    alarms_page = 2 + len(radio)
    external_page = alarms_page + 1
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
          ('620','Ring all Cisco phones','Calls primary lines; DND phones are skipped.'),
          ('621','Incoming calls group','Calls configured group members. Requires enabled group.'),
          ('630','Home Assistant Assist','Uses the configured voice pipeline. Requires enabled Assist.'),
          ('640','Manual tone test','Finite tone, not a scheduled alert. Requires enabled alarms.'),
          ('641','Toggle saved alarm','Enable/disable your saved alarm. Requires phone services.')]
    table(c,rows,462,32)
    block(c,'Mac SIP client - 209', 'Use the configured SIP account over TCP. Keep the Mac awake and the app running to receive calls. Group 620 contains Cisco phones; group 621 may include a Mac if configured. Number 641 controls saved alarms for physical Cisco phones only.',178,BLUE)
    paragraph(c,f'Radio: pages 2-{alarms_page-1}. Alarms and controls: page {alarms_page}. External tests and CLI: page {external_page}. These are example line assignments; check your local directory.',42,82,528,9,12,GRAY)
    footer(c,1);c.showPage()
    for index, (rows, heights) in enumerate(radio):
        heading = 'Music & radio' if index == 0 else 'Music & radio (continued)'
        title(c, 'RADIO ON DEMAND', heading, 'Dial a station, listen, then hang up. Streams run only while a call is active.')
        table(c, rows, 631, row_heights=heights)
        block(c, "Children's channels 708-710", "708: toddler music. 709: lullabies. 710: children's songs. Type the full three-digit number, then press Dial. Only stations enabled by your administrator appear in the live directory.", 190, BLUE)
        paragraph(c, 'Radio needs Internet access. The default call limit is 30 minutes; an administrator can set up to 2 hours. Hang up to stop. An unavailable stream gives a short tone and ends the call. Echo online is the current online service, not the former Moscow FM broadcast.', 42, 102, 528, 9, 12, GRAY)
        footer(c, 2 + index); c.showPage()
    title(c, 'CONTROL FROM HOME ASSISTANT', 'Calls, alarms & messages', 'Updated alerts require the matching gateway and Home Assistant integration.')
    y=640
    y=block(c,'Set an alarm for selected days', 'Choose the time and weekdays in the Home Assistant timezone, then enable the alarm. One recurring alarm slot is shared by both lines of a physical phone. Alerts keep ringing until manually answered, rejected or stopped; they never auto-answer.',y)
    y=block(c,'641 - toggle the saved alarm from your Cisco phone', 'After an alarm has been configured, dial 641 to enable or disable it. Listen for confirmation. It does not invent an alarm time or change its saved days or line. If no alarm is configured, it stays off. Requires enabled phone services and the matching HA integration.',y,BLUE)
    y=block(c,'Start or stop a timer', 'Set the duration and press Start timer. At zero, the phone rings until you answer, reject it or press Stop alert. Cancel timer cancels the countdown and stops its active alert. Busy, offline or DND phones can miss alerts; recovery does not replay them.',y)
    y=block(c,'Call controls', 'Choose the line and allowed destination, then use Dial. HA Answer handles normal ringing calls, not timer/alarm alerts. Hang up ends the selected active call. Speaker toggles a key; HA cannot reliably report the physical speaker or handset state.',y)
    y=block(c,'Do Not Disturb and auto-answer', 'Gateway DND blocks new calls and alerts without cancelling saved schedules. It is separate from the phone\'s native DND icon. Optional auto-answer applies to normal calls. Timers and alarms always require manual dismissal.',y)
    block(c,'Send text, photos or still sprites', 'Send a text page or a local PNG/JPEG through the integration. Pictures fit the 298 x 156 Services pane. Press Exit to return. These are local screen notifications, not mobile SMS/MMS; animation and video are not supported.',y)
    paragraph(c,'For voice assistant 630, discover each phone in HA, enable Allow calls and select its voice pipeline. The Russian Baya pipeline must be configured explicitly.',42,85,528,9,12,GRAY)
    footer(c,alarms_page);c.showPage()
    title(c, 'OPTIONAL SERVICES AND THE COMMAND LINE', 'External tests & CLI', 'Service routes appear in the live catalog only when enabled and allowed.')
    rows=[('801','Callcentric peer','Optional configured peer; not a universal public test number.'),
          ('802','SIP2SIP echo test','Requires a configured SIP2SIP route to test service 4444.'),
          ('803','The Test Call','English menu. Requires a Linphone route to thetestcall.')]
    table(c,rows,631,43)
    y=block(c,'803 - The Test Call English menu', 'While connected: 1 = record and playback, 2 = digit test, 3 = echo test, 4 = music. Dial *1001 inside the call for UTC time. This public test service can change or be unavailable; hang up when finished.',449,BLUE)
    y=block(c,'Check the complete local catalog', 'Run cisco directory to see phone labels, local extensions, enabled radio stations and approved service shortcuts. Run cisco 213 to read that line\'s current status and available command examples. Reading status does not place a call.',y)
    y=block(c,'Call or connect both speakers', 'cisco 213 220 places a normal call. cisco 213 intercom 220 (alias drop-in) requests speaker answer on two distinct idle Cisco phones. Both must be available, with DND off and no active alerts. End with cisco 213 hangup.',y)
    block(c,'Timers and weekday alarms from the CLI', 'cisco 213 timer start 5 rings in five minutes. cisco 213 alarm set 07:30 --days weekdays --enable saves a weekday alarm in the HA timezone. Scheduling needs the optional private HA configuration; check cisco 213 schedule first.',y)
    paragraph(c,'Free SIP-to-SIP service is not free PSTN calling. These example external routes provide no emergency service. Keep accounts, real room labels and exported contacts private. Setup and troubleshooting: github.com/4alvit/cisco-home-telephony',42,91,528,9,12,GRAY)
    footer(c,external_page);c.save()
    print(target)
    if args.html_output:
        html_target = Path(args.html_output); html_target.parent.mkdir(parents=True, exist_ok=True)
        html_target.write_text(radio_html(stations), encoding='utf-8')
        print(html_target)


if __name__=='__main__':main()
