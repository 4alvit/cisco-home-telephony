#!/usr/bin/env node
/** Editable technical deck and SVG diagrams. See docs/slides/README.md. */
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const out = path.join(root, 'docs/slides');
const diagramOut = path.join(root, 'docs/diagrams');
const build = path.join(root, '.slides-build');
const W=1280, H=720;
const C={navy:'#101C34',teal:'#48DDCC',coral:'#FF836D',yellow:'#F7CB64',white:'#F5F7FB',muted:'#AAB9D0',ink:'#172841',line:'#536780'};
const FONT='Arial';
const S={
 firmware:'https://www.cisco.com/web/software/282074288/128566/cmterm-7945_7965-sip.9-4-2SR2-2-readme.html',
 upgrade:'https://www.cisco.com/en/US/docs/voice_ip_comm/cuipph/7900_series/firmware/9_3_1SR4/english/release_notes/P790_BK_RF3660D6_00_rn-9_3_1sr4-7900-series_chapter_00.pdf',
 power:'https://www.cisco.com/c/en/us/support/docs/voice-unified-communications/unified-ip-phone-7900-series/97869-poe-requirement-faq.html',
 pjsip:'https://docs.asterisk.org/Configuration/Channel-Drivers/SIP/Configuring-res_pjsip/res_pjsip-Configuration-Examples/',
 k3s:'https://docs.k3s.io/installation/requirements',
 kube:'https://kubernetes.io/docs/concepts/workloads/controllers/deployment/',
 ha:'https://github.com/ha-homelab/asterisk-telephony',
 voip:'https://www.home-assistant.io/integrations/voip/',
 upstream:'https://github.com/TECH7Fox/asterisk-hass-integration/tree/65ea9d637a58d6edb3b2bc9e8a6f90de896a9b3e',
 opus:'https://github.com/wazo-platform/wazo-codec-opus-open-source/tree/591b6cd9b75e2b9b6d815ef364a385fccee595a7'
};
await Promise.all([out,diagramOut,build].map(p=>fs.mkdir(p,{recursive:true})));
const rect=(id,x,y,w,h,label,accent=C.teal)=>({id,x,y,w,h,label,accent});
const edge=(from,to,label='',color=C.teal,fromSide='right',toSide='left',both=false,labelAt=null)=>({from,to,label,color,fromSide,toSide,both,labelAt});
const diagrams={
 'architecture':{title:'Cisco, Asterisk and Home Assistant',description:'PoE powers the phones, which call through Asterisk. Home Assistant connects to AMI. Assist receives SIP/RTP from Asterisk. The existing DHCP server remains in use. TFTP runs only during provisioning.',
 nodes:[rect('phones',45,185,265,120,'Cisco 7945G\nPoE 802.3af'),rect('switch',455,185,290,120,'PoE switch\nVoice LAN / VLAN'),rect('ast',900,185,330,120,'Asterisk / PJSIP\nstatic IP'),rect('router',455,440,290,110,'Router\nDHCP option 150',C.yellow),rect('tftp',45,440,265,110,'Temporary TFTP\nfirmware and XML',C.coral),rect('ha',900,440,330,110,'Home Assistant\nAMI + VoIP / Assist',C.yellow)],
 edges:[edge('phones','switch','Ethernet',C.teal,'right','left',true),edge('switch','ast','SIP / RTP',C.teal,'right','left',true),edge('router','switch','DHCP',C.yellow,'top','bottom',false,[665,382]),edge('tftp','phones','download',C.coral,'top','bottom',false,[235,382]),edge('ast','ha','AMI\nSIP / RTP',C.yellow,'bottom','top',true,[1160,360])],
 footer:'Asterisk uses hostNetwork and one replica. HA opens the AMI connection to the PBX.\nTFTP runs only during provisioning. SIP, RTP and AMI stay on the LAN / VPN.'},
 'assist':{title:'630: voice assistant on the phone',description:'Asterisk converts Cisco G.711 audio to Opus for the built-in Home Assistant VoIP integration. AMI carries no audio. Test speech recognition and two-way audio on the pilot phone.',
 nodes:[rect('cisco',45,210,280,140,'Cisco 7945G\nextension 201\nG.711'),rect('codec',480,210,315,140,'Asterisk\nG.711 ↔ Opus\nextension 630',C.coral),rect('assist',950,210,285,140,'Home Assistant\nVoIP / Assist\nOpus',C.yellow)],
 edges:[edge('cisco','codec','SIP TCP\nRTP',C.teal,'right','left',true,[402,180]),edge('codec','assist','SIP UDP\nRTP',C.yellow,'right','left',true,[873,180])],
 annotations:[{x:45,y:435,w:1190,h:85,text:'First call: enable Allow calls and select a pipeline in HA.\nThen test speech recognition, the spoken reply and hangup.',size:28,color:C.white}],
 footer:'SIP From retains the phone extension. HA uses dynamic UDP ports for RTP / RTCP.\nA call on the pilot phone verifies audio. Pod readiness checks server components.'},
 'commissioning':{title:'One pilot phone before the full fleet',description:'Follow the intermediate upgrade steps required by the starting firmware. Confirm the resulting load on the phone screen.',
 nodes:[rect('inventory',45,195,290,120,'Inventory\nPID / HW / Load',C.yellow),rect('firmware',480,195,290,120,'Firmware path\nSCCP to SIP',C.coral),rect('tftp',915,195,290,120,'TFTP ready\nbundle + SEP XML'),rect('pilot',915,430,290,120,'One pilot phone\nstable power'),rect('accept',480,430,290,120,'Call tests\naudio / DTMF / reboot'),rect('fleet',45,430,290,120,'Remaining phones\none at a time',C.yellow)],
 edges:[edge('inventory','firmware'),edge('firmware','tftp'),edge('tftp','pilot','',C.teal,'bottom','top'),edge('pilot','accept','',C.teal,'left','right'),edge('accept','fleet','',C.yellow,'left','right')],
 footer:'HW 13.0+ requires at least 9.3(1)SR3. The upgrade path depends on the current load.\nPrepare TFTP and recovery before a reset. Keep power connected during flash writes.'},
 'power':{title:'7945G has built-in PoE support',description:'The phone already supports 802.3af. Add an active IEEE 802.3af injector if the switch does not supply PoE.',
 nodes:[rect('poe',60,185,300,120,'PoE switch\nIEEE 802.3af'),rect('phone1',900,185,310,120,'Cisco 7945G\nSW / Network port'),rect('lan',60,440,300,115,'Standard switch\nEthernet',C.yellow),rect('injector',480,440,300,115,'Active injector\nIEEE 802.3af',C.coral),rect('phone2',900,440,310,115,'Cisco 7945G\nSW / Network port')],
 edges:[edge('poe','phone1','data + power'),edge('lan','injector','data',C.yellow),edge('injector','phone2','PoE')],
 footer:'A passive 24 V injector is unsuitable. The UPS also needs to power the switch, router and k3s node.'}
};
const esc=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
function port(n,side){return side==='right'?[n.x+n.w,n.y+n.h/2]:side==='left'?[n.x,n.y+n.h/2]:side==='top'?[n.x+n.w/2,n.y]:[n.x+n.w/2,n.y+n.h];}
function svgText(text,x,y,size,color,anchor='start',weight=400){return `<text x="${x}" y="${y}" font-family="Arial, sans-serif" font-size="${size}" fill="${color}" font-weight="${weight}" text-anchor="${anchor}">${String(text).split('\n').map((v,i)=>`<tspan x="${x}" dy="${i?size*1.3:0}">${esc(v)}</tspan>`).join('')}</text>`;}
function diagramSvg(d){let s=`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" role="img" aria-labelledby="title desc"><title id="title">${esc(d.title)}</title><desc id="desc">${esc(d.description)}</desc><defs>${[C.teal,C.coral,C.yellow].map(c=>`<marker id="arrow-${c.slice(1)}" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto-start-reverse"><path d="M0,0 L8,4 L0,8" fill="${c}"/></marker>`).join('')}</defs><rect width="1280" height="720" fill="${C.navy}"/>${svgText(d.title,45,55,34,C.white,'start',700)}`;
 for(const e of d.extras??[])s+=`<rect x="${e.x}" y="${e.y}" width="${e.w}" height="${e.h}" rx="12" fill="none" stroke="${e.color}" stroke-dasharray="7 7"/>${svgText(e.label,e.x+16,e.y+32,20,e.color)}`;
 for(const e of d.edges){const a=port(d.nodes.find(n=>n.id===e.from),e.fromSide),b=port(d.nodes.find(n=>n.id===e.to),e.toSide),mid=(a[0]+b[0])/2;const dpath=a[1]===b[1]||a[0]===b[0]?`M${a} L${b}`:`M${a} H${mid} V${b[1]} H${b[0]}`;s+=`<path d="${dpath}" fill="none" stroke="${e.color}" stroke-width="3" ${e.both?`marker-start="url(#arrow-${e.color.slice(1)})"`:''} marker-end="url(#arrow-${e.color.slice(1)})"/>`;if(e.label){const lp=e.labelAt??[mid,(a[1]+b[1])/2-16];s+=svgText(e.label,lp[0],lp[1],19,e.color,'middle');}}
 for(const n of d.nodes)s+=`<rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="10" fill="${C.ink}" stroke="${n.accent}" stroke-width="2"/>${svgText(n.label,n.x+n.w/2,n.y+n.h/2-(n.label.split('\n').length-1)*16+8,24,C.white,'middle',600)}`;
 for(const a of d.annotations??[])s+=svgText(a.text,a.x,a.y+a.size,a.size,a.color);
 s+=svgText(d.footer,45,660,19,C.muted);return s+'</svg>\n';}
for(const [name,d] of Object.entries(diagrams))await fs.writeFile(path.join(diagramOut,name+'.svg'),diagramSvg(d));
if(process.argv.includes('--svg-only')){console.log('SVG diagrams written to docs/diagrams');process.exit(0);}
let artifact;
try{artifact=process.env.ARTIFACT_TOOL_ROOT?await import(pathToFileURL(path.join(process.env.ARTIFACT_TOOL_ROOT,'dist/artifact_tool.mjs')).href):await import('@oai/artifact-tool');}catch(e){throw new Error('PowerPoint build requires @oai/artifact-tool from the Codex workspace runtime. Set ARTIFACT_TOOL_ROOT to that package directory. SVG-only build needs only Node.js.',{cause:e});}
const {Presentation,PresentationFile}=artifact;
const p=Presentation.create({slideSize:{width:W,height:H}});
const bytes=async(f,b)=>fs.writeFile(f,new Uint8Array(await b.arrayBuffer()));
function text(sl,t,x,y,w,h,size=28,color=C.white,bold=false,align='left'){
 const q=sl.shapes.add({name:t.substring(0,65),geometry:'textbox',position:{left:x,top:y,width:w,height:h},fill:'none',line:{fill:'none',width:0}});q.text=t;q.text.style={fontSize:size,typeface:FONT,color,bold,alignment:align,verticalAlignment:'top',insets:0,autoFit:'none',wrap:'square'};return q;
}
function box(sl,n,scale=1,dx=0,dy=0){const q=sl.shapes.add({name:n.id,geometry:'roundRect',position:{left:n.x*scale+dx,top:n.y*scale+dy,width:n.w*scale,height:n.h*scale},fill:C.ink,line:{fill:n.accent,width:2},borderRadius:10});q.text=n.label;q.text.style={fontSize:24*scale,typeface:FONT,color:C.white,bold:true,alignment:'center',verticalAlignment:'middle',insets:8,autoFit:'none'};return q;}
function base(title,notes=''){const sl=p.slides.add();sl.background.fill=C.navy;text(sl,title,60,44,1150,105,45,C.white,true);text(sl,String(p.slides.items.length).padStart(2,'0'),1160,668,60,22,17,C.muted,false,'right');sl.speakerNotes.text=notes;return sl;}
function label(sl,t,x,y,w=1060,color=C.teal,size=29){text(sl,t,x,y,w,86,size,color,true);}
function para(sl,t,x,y,w=1060,size=28,color=C.white,h=110){text(sl,t,x,y,w,h,size,color);}
function foot(sl,t){text(sl,t,60,625,1070,48,19,C.muted);}
function diagramSlide(name,notes){const d=diagrams[name],sl=base(d.title,notes);const shapes=new Map();const sc=name==='architecture'?.90:.90,dx=64,dy=35;
 for(const e of d.extras??[]){sl.shapes.add({geometry:'roundRect',position:{left:e.x*sc+dx,top:e.y*sc+dy,width:e.w*sc,height:e.h*sc},fill:'none',line:{fill:e.color,width:1,style:'dashed'}});text(sl,e.label,e.x*sc+dx+14,e.y*sc+dy+13,e.w*sc-20,35,20,e.color);}
 for(const n of d.nodes)shapes.set(n.id,box(sl,n,sc,dx,dy));
 for(const e of d.edges){sl.shapes.connect(shapes.get(e.from),shapes.get(e.to),{kind:'straight',fromSide:e.fromSide,toSide:e.toSide,line:{fill:e.color,width:3},...(e.both?{head:{type:'triangle',width:'sm',length:'sm'}}:{}),tail:{type:'triangle',width:'sm',length:'sm'}});if(e.label){const a=port(d.nodes.find(n=>n.id===e.from),e.fromSide),b=port(d.nodes.find(n=>n.id===e.to),e.toSide);const lp=e.labelAt??[(a[0]+b[0])/2,(a[1]+b[1])/2-16];text(sl,e.label,lp[0]*sc+dx-95,lp[1]*sc+dy-22,190,60,18,e.color,false,'center');}}
 for(const a of d.annotations??[])text(sl,a.text,a.x*sc+dx,a.y*sc+dy,a.w*sc,a.h*sc,a.size*sc,a.color);
 foot(sl,d.footer.replaceAll(' · ','  /  '));return sl;}
// 01: Minimal title, editable typography.
{
 const sl=base('', 'Cisco 7945G phones provide a color display, two line buttons and native IEEE 802.3af PoE. This project uses one or two independently authenticated SIP lines per phone, Asterisk in k3s and Home Assistant. Verify the model, hardware revision and installed firmware on each physical phone before generating its configuration. These slides describe the architecture and deployment process. The acceptance record separates component tests from actual call and audio results.');
 text(sl,'Cisco 7945G',60,135,1160,115,80,C.teal,true);text(sl,'Home telephony',60,265,1160,105,61,C.white,true);text(sl,'Asterisk in k3s + Home Assistant',60,400,1100,75,41,C.coral);text(sl,'Two SIP lines, color display and native PoE',60,540,1100,60,29,C.muted);}
// 02: Inventory facts, not a generic product comparison.
{
 const sl=base('Cisco 7945G: model and inventory',`The Cisco 7945G has two line buttons, a color display and native IEEE 802.3af PoE. The generator supports one or two independent SIP lines with separate credentials, preserving existing passwords when another line is added. Confirm PID, VID/HW, MAC and firmware directly on each phone. Cisco firmware family 7945_7965: ${S.firmware}\nPoE: ${S.power}\nCisco provides SIP firmware for this model. Verify the required Asterisk features on the pilot phone.`);
 text(sl,'2',65,185,250,190,132,C.teal,true);text(sl,'SIP lines',70,380,250,55,28,C.muted);text(sl,'Native 802.3af',70,465,265,55,25,C.teal);
 label(sl,'7945_7965 SIP firmware family',385,180,810,C.white,33);para(sl,'A phone running SCCP needs SIP firmware.\nThis model does not need MPP conversion.',385,290,805,27,C.muted,110);
 label(sl,'Before starting',385,465,790,C.yellow,28);para(sl,'Record PID, VID/HW, MAC, App Load,\nBoot Load, IP and TFTP for every phone.',385,520,805,27,C.white,100);
}
// 03: Architecture.
diagramSlide('architecture',`The design uses one Asterisk replica on a designated LAN node with hostNetwork and a static LAN IP. Keep the existing router DHCP service. Run TFTP only when needed. HA opens TCP AMI to Asterisk. For Assist, Asterisk opens SIP to HA VoIP and audio flows in both directions. Home Assistant may run on another LAN node or across the existing VPN. HTTP Ingress does not transport these protocols. Kubernetes: ${S.kube}\nPJSIP: ${S.pjsip}\nHA: ${S.ha}\nVoIP: ${S.voip}\nSee docs/k3s-deployment.md and docs/home-assistant.md.`);
// 04: Ports as flat typographic rows.
{
 const sl=base('Signaling and audio ports',`PBX ports in this project: SIP TCP 5060 by default, optional UDP 5060, RTP UDP 10000–10199. tftpd-hpa uses UDP 69 and UDP 20000–20031. Allow AMI TCP 5038 only from the actual HA and optional phone-gateway source IPs/32. HA VoIP listens on SIP UDP 5060 or the selected port, plus dynamic RTP/RTCP UDP ports restricted to the PBX. Cisco RTP uses UDP 16384–16482. Firewall rules must account for both directions. NetworkPolicy alone does not protect hostNetwork. PJSIP: ${S.pjsip}\nVoIP: ${S.voip}\nSee docs/home-assistant.md.`);
 const rows=[['5060','TCP / UDP','SIP: Cisco TCP, HA UDP',C.teal],['10000–10199','UDP on PBX','RTP / audio',C.teal],['5038','TCP on PBX','AMI: HA / gateway /32',C.yellow],['69 + 20000–20031','UDP on PBX','Temporary TFTP',C.coral]];
 rows.forEach((r,i)=>{const y=170+i*102;text(sl,r[0],60,y,490,60,r[0].length>11?33:41,r[3],true);text(sl,r[1],570,y+7,225,45,26,C.white);text(sl,r[2],805,y+7,420,60,25,C.muted);});
 foot(sl,'HA uses dynamic RTP / RTCP ports. Restrict firewall rules to the required source IPs.');
}
// 05: Native editable power diagram.
diagramSlide('power',`Cisco 7945G supports IEEE 802.3af: ${S.power}\nIf the existing switch does not supply PoE, use an active IEEE 802.3af injector or a PoE switch. The phone needs no hardware modification. Connect to the SW/Network port. Check the per-port and total switch power budgets. To maintain service during a power outage, the UPS must power the entire network path.`);
// 06: Native editable conversion flow.
diagramSlide('commissioning',`The appropriate reset procedure depends on the current phone state. Check App Load and the required upgrade path first. Cisco README for the 7945_7965 family: ${S.firmware}\nNever downgrade HW 13.0+ below 9.3(1)SR3. Do not apply an intermediate 8.5(2) load to the entire fleet. Phones starting below 8.3(3) require an intermediate 8.3(3)–8.5(2) load before 9.3(1)+, subject to the selected release notes: ${S.upgrade}\nObtain the Cisco bundle from an authorized source and record its SHA-256 locally. The .loads file alone is insufficient.`);
// 07: TFTP specifics.
{
 const sl=base('TFTP preparation before restart',`Cisco: ${S.firmware}\nChoose the exact loadInformation value from the selected bundle README and .loads file. Do not substitute a 7942/7962 file. SEP filenames use the uppercase MAC address without separators. The configuration may contain a SIP password, so restrict TFTP to the trusted LAN. Configure DHCP option 150 on the existing DHCP server, or use Alternate TFTP on the phone.`);
 label(sl,'Complete official firmware bundle',60,180,1100,C.teal,34);para(sl,'.loads and every referenced binary file\nwith the original filenames and letter case',60,245,1100,30,C.white,95);
 label(sl,'SEP<MAC>.cnf.xml',60,390,1100,C.coral,34);para(sl,'SIP server, extension and a unique password\nLoad ID from the selected 7945_7965 firmware branch',60,454,1100,29,C.white,105);
 foot(sl,'Use DHCP option 150 on the home router or Alternate TFTP on the phone.');
}
// 08: Local SIP config.
{
 const sl=base('Asterisk: independent SIP lines',`PJSIP examples: ${S.pjsip}\nProject baseline: one or two SIP lines per physical Cisco 7945G. Each line has a separate endpoint, auth, AOR and unique password. The phone XML preserves line button order and uses the matching transport. Legacy single-line inventory remains supported. Keep existing secrets when adding a second line. Omit lineIndex to avoid CUCM bulk-registration behavior. TCP is the default. Enable UDP for phones explicitly. Use G.711 ulaw/alaw and direct_media=no to keep RTP flowing through Asterisk. Add NAT settings only when needed. The initial configuration supports internal calls without a PSTN/SIP trunk. Test extensions: 600 echo, 601 tone, 620 ring group, and 630 Assist when HA integration is enabled.`);
 text(sl,'201 / 202',60,195,550,100,72,C.teal,true);text(sl,'one phone',730,207,485,85,49,C.coral,true);
 para(sl,'One endpoint, auth and AOR per line\nA unique password for each line',60,355,1100,31,C.white,100);
 para(sl,'G.711 ulaw / alaw\ndirect_media=no keeps RTP on Asterisk',60,495,1100,29,C.muted,100);
 foot(sl,'600 echo, 601 tone, 620 ring group. Extension 630 requires Assist.');
}
// 09: AMI monitoring and call control are separate from voice transport.
{
 const sl=base('AMI: status and call control',`HA installation and package: ${S.ha}\nVerified TECH7Fox Asterisk integration v1.0.5, pinned commit: ${S.upstream}\nHA opens TCP 5038 to the PBX without TLS. Restrict access to the actual source IP/32 over LAN or VPN. Keep the separate .local/ha-ami-secret.json credentials out of Git and phone XML. Permissions are read=call,dtmf and write=system, plus originate only with allow_originate:true. Upstream needs system for discovery, but that permission also allows Hangup and other administrative actions. This is privileged access. HA exposes one global asterisk.send_action service without multi-PBX selection. DTMF uses a timestamp state and Digit attribute. Sent/Received directions are relative to Asterisk. Registered derives from DeviceState and does not replace pjsip show contacts. Map actual entity IDs and verify with two phones.`);
 label(sl,'Home Assistant connects to PBX:5038',60,175,1110,C.teal,34);
 para(sl,'Status, connected line and DTMF\nActual entity IDs from your HA instance',60,238,1110,29,C.white,100);
 label(sl,'Place calls and hang up a specific channel',60,385,1110,C.yellow,33);
 para(sl,'asterisk.send_action: Originate / Hangup\nThe DTMF example sends a harmless notification',60,451,1110,28,C.white,110);
 foot(sl,'Use a separate secret and a /32 ACL. The system permission allows administrative actions.');
}
// 10: Native editable voice path, also exported as standalone SVG.
diagramSlide('assist',`Official HA VoIP integration: ${S.voip}\nInstallation and acceptance: ${S.ha}\nPinned Opus transcoder: ${S.opus}\nCisco 7945G uses G.711. The installed Home Assistant VoIP integration requires Opus. Asterisk transcodes audio in both directions through a module built from Wazo source. Translation-path and container smoke tests verify server components. Verify a real conversation on the household phone separately. SIP to Cisco defaults to TCP 5060. SIP to HA uses UDP 5060 or the selected port. PBX RTP uses 10000–10199. HA uses dynamic RTP/RTCP UDP ports. Use the primary line of each phone. Enable Allow calls and select its Russian Baya pipeline for the discovered VoIP device; repeat this one-time setup for each physical phone. Generic phone controls do not complete native voice setup. SIP From preserves the extension, including calls originated from HA. Test STT, the TTS reply, hangup and a repeat call. AMI carries events and commands, not audio.`);
// 11: Deployment stages do not claim a completed household installation.
{
 const sl=base('Deployment and commissioning',`See README.md, docs/pbx-bootstrap.md, docs/k3s-deployment.md, docs/home-assistant.md, scripts/ and deploy/. Namespace: telephony. Bootstrap prepares server components, images and network rules before connecting an inventoried phone. provision.py --pbx-only performs this step without fake MAC addresses, phone endpoints or TFTP. Keep allow_originate:false and assist.enabled:false until the connection prerequisites pass. Do not commit local site, inventory or secrets. TFTP requires an actual phone inventory and suitable verified firmware. Take the exact generation and deployment commands from the current runbook. One Asterisk replica with Recreate prevents hostNetwork port conflicts. Stage the HA component before a separate installation window on the existing HA instance. k3s: ${S.k3s}\nHA: ${S.ha}`);
 const steps=[['01','Server bootstrap','Images, static IP, firewall, Asterisk and Opus'],['02','Verified pilot phone','PID / HW / Load / MAC, SIP profile, temporary TFTP'],['03','Home Assistant setup','AMI /32, secret, actual entity IDs and VoIP pipeline'],['04','Acceptance and rollout','Two phones, a call to 630, then the remaining phones']];
 steps.forEach((a,i)=>{let y=165+i*111;text(sl,a[0],60,y,80,60,40,[C.teal,C.coral,C.yellow,C.teal][i],true);text(sl,a[1],180,y,1000,48,32,C.white,true);text(sl,a[2],180,y+49,1000,45,25,C.muted);});
}
// 12: Physical phone acceptance remains explicit.
{
 const sl=base('Acceptance tests on real phones',`These are project acceptance criteria. Record actual household installation results separately. Use two physical phones with verified inventory. Check pjsip show contacts, calls between different physical handsets, such as 201 to 203 and 203 to 201, two-way audio, DTMF/echo, hold/transfer and the clock. Then check HA states, Originate, Hangup using the actual active Channel, and a harmless DTMF notification. Determine DTMF Sent/Received direction on the pilot. For 630: Allow calls, pipeline selection, a separate identity for each phone, STT, TTS and hangup. Repeat after a phone cold boot and controlled PBX/HA restarts. Automated runtime and smoke tests do not replace physical acceptance. ${S.ha}/blob/main/docs/acceptance.md`);
 const steps=[['Telephony','201 and 203: cross-phone audio, hold / transfer',C.teal],['Home Assistant','States, Originate, Hangup on the correct Channel, DTMF',C.yellow],['Assist 630','Speech recognition, spoken reply and hangup',C.coral]];
 steps.forEach((a,i)=>{const y=177+i*139;label(sl,a[0],60,y,1110,a[2],34);para(sl,a[1],60,y+61,1110,27,C.white,63);});
 foot(sl,'Repeat after phone, PBX and HA restarts. Readiness does not verify audio.');
}
// 13: Operations.
{
 const sl=base('Operations and recovery',`Project operating requirements: telephony runs on one node without high availability. Back up inventory, SIP/AMI secrets, configuration, persistent Asterisk data, firmware provenance, HA config/.storage and entity mappings. Test restoration separately. Do not publish SIP/AMI passwords or firmware binaries. Exclude raw DTMF and call sensors from HA Recorder. Exclusions do not remove existing history. Setting home_assistant.enabled:false and regenerating/applying the configuration disables AMI and extension 630 while retaining basic internal calls. See docs/operations.md and ${S.ha}/blob/main/docs/privacy.md`);
 label(sl,'Private backups outside Git',60,185,1110,C.teal,34);para(sl,'Inventory, SIP / AMI secrets and Asterisk data\nHA config / .storage, entity mappings and image versions',60,250,1110,29,C.white,100);
 label(sl,'One node is a single point of failure',60,410,1110,C.coral,34);para(sl,'UPS for network equipment and the k3s node\nA recovery plan that preserves the LAN IP',60,475,1110,29,C.white,100);
 foot(sl,'Exclude raw DTMF and connected-line data from Recorder. Review existing history separately.');
}
// 14: End with an explicit distinction between preparation and acceptance.
{
 const sl=base('Server readiness and phone acceptance', 'Record actual hardware results in a separate private acceptance log. These slides describe preparation and acceptance criteria. Inventory each device to record PID/HW/MAC, App Load, network addresses and power. Automated tests verify components and configuration. Physical acceptance covers audio, HA call control, DTMF and Assist on real phones. After the pilot passes, add the remaining phones one at a time.');
 label(sl,'Server preparation can come first',60,180,1110,C.teal,36);
 para(sl,'Images, configuration, AMI and Opus\nTests verify the server components',60,248,1110,29,C.white,100);
 label(sl,'A pilot verifies the phone experience',60,398,1110,C.coral,36);
 para(sl,'Phone inventory, a two-party call and Assist\nAdd the remaining phones after acceptance',60,466,1110,29,C.white,100);
 foot(sl,'Physical acceptance is a separate step. Keep the results in the private deployment log.');
}
// 15: Retain every physical identity when handsets are disconnected.
{
 const sl=base('Ten Cisco identities, twenty lines', `Ten configured Cisco identities retain independent SIP pairs 201/202, 203/204, 205/206, 207/208, 210/211, 212/213, 214/215, 216/217, 218/219 and 220/221. A Mac SIP client uses 209 separately, for 21 local endpoints in total. Configuration and credentials are retained when devices are unplugged or moved; current registration depends on which physical devices are connected. The tenth device completed an automatic upgrade without a PoE cycle. Its actual SIP application and SIP45.9-4-2SR4-3S version, both registrations, numeric labels, saved LakeDawn wallpaper, authenticated CGI and independently pinned SSH login were verified. Earlier added devices passed the same physical checks. Temporary TFTP was removed, the selected source window closed and its timer stopped without disturbing other firewall rules. Per-line call and Assist acceptance is recorded separately. The first four handsets previously registered all eight lines and passed local Echo 600/HA Hangup checks. Ring group 620 uses only each Cisco device's primary line and skips PBX DND. Mac 209 is excluded. See docs/adding-phones.md and docs/server-validation.md for dated evidence.`);
 const phones=['201 / 202','203 / 204','205 / 206','207 / 208','210 / 211','212 / 213','214 / 215','216 / 217','218 / 219','220 / 221'];
 phones.forEach((a,i)=>{const x=60+(i%5)*244,y=177+Math.floor(i/5)*151;text(sl,'PHONE '+(i+1),x,y,220,35,18,C.muted);text(sl,a,x,y+45,220,60,34,C.teal,true);});
 label(sl,'620: one primary line per Cisco device',60,513,1150,C.white,30);
 para(sl,'201 · 203 · 205 · 207 · 210 · 212 · 214 · 216 · 218 · 220',60,562,1150,26,C.muted,44);
 foot(sl,'Keep each device’s identity and credentials when unplugged. Online status follows the connected phones.');
}
// 16: Capabilities are explicit, while live acceptance remains a separate claim.
{
 const sl=base('Home Assistant phone controls', `The ten-phone Home Assistant installation has 290 controls and 143 Recorder exclusions. Post-restart verification preserved all 6,818 pre-expansion registry identities and confirmed three additional text privacy gates. Dashboard readback passed. Current online counts depend on which phones are connected. New per-line Echo, Hangup and native Assist acceptance is tracked separately. Native Assist for primary 218 remains undiscovered because its handset was unplugged before onboarding. Each phone requires its first 630 call, Allow calls and its Russian Baya pipeline; generic control installation is not voice acceptance. The revised alert contract is deployed with integration 0.2. Physical timer alerts ended when the user answered in two checks. A scheduled weekday alarm rang on a physical handset for more than 90 seconds. The corrected HA Stop alert route passed live acceptance and ended its owned alert. The Mac echo test also passed, including human listening. The updated HA integration removes Alarm sound and Timer sound selectors and adds Stop alert. Timers and weekday alarms keep ringing until a person answers or rejects at the phone, presses Stop alert, cancels the active timer, or disables the active alarm. They never auto-answer and play no audio after answering. HA Answer is blocked for alert channels. The gateway persists alert identities and reconciles the same channels after a gateway restart without replaying events after PBX or power loss. Normal calls retain optional auto-answer, off by default. The separate TECH7Fox Asterisk and native HA VoIP integrations remain in use. Earlier finite-tone tests do not validate the new behavior. Existing saved pilot evidence covers normal gateway calls, exact-channel Hangup, DND and displayed text/PNG content. Sources: docs/server-validation.md, docs/phone-gateway.md and https://github.com/ha-homelab/asterisk-telephony/blob/main/docs/phone-controls.md .`);
 text(sl,'290',60,180,390,175,126,C.teal,true);text(sl,'controls across ten phones',65,358,390,95,28,C.muted);
 const rows=[['Call controls','Line, dial, answer and exact-channel hangup',C.white],['Personal settings','PBX DND and optional normal-call auto-answer',C.yellow],['Schedules','Timers and weekdays with a Stop alert control',C.coral],['Phone screen','Authenticated text and approved images',C.teal]];
 rows.forEach((a,i)=>{const y=165+i*103;text(sl,a[0],485,y,730,45,30,a[2],true);text(sl,a[1],485,y+48,730,45,24,C.muted);});
 foot(sl,'Manual timer dismissal and HA Stop passed. A scheduled alarm rang beyond 90 seconds.');
}
// 17: Public station names and dial numbers, without stream credentials or private URLs.
{
 const sl=base('Radio and scheduled alerts', `The new timer/alarm contract rings until a person answers, rejects or stops the alert. It never auto-answers or plays after-answer audio. The revised behavior is deployed. Manual physical timer dismissal and the corrected HA Stop alert route passed live checks. A scheduled weekday alarm rang on a physical phone beyond 90 seconds. Two timer checks ended by manual answer, with no gateway auto-answer operation. A private Asterisk Local helper omits Dial timeout, ignores forwarding and uses WaitForSignal without an interval. The five-second AMI timeout bounds only the initial Local answer, not handset ringing. Manual 640 remains a finite tone test and the old 20-second ring API remains a legacy finite call, neither used by the new scheduler. Radio is unchanged: an opt-in FFmpeg/AudioSocket worker runs only for the active call, with a default 30-minute limit and two concurrent streams. All seven official streams decoded in the saved probe, and radio 701 reached Up with increasing bidirectional RTP counters and exact-channel cleanup. The three new Deti FM channels 708–710 also decoded through the installed worker, and a listener confirmed music on 708. This does not establish audible playback for every station. Sources: docs/server-validation.md and docs/radio-and-alarms.md . Official stations: https://matryoshka.fm/ , https://echofm.online/ , https://streams.radiobob.de/ , https://somafm.com/poptron/directstreamlinks.html , https://www.radyoviva.com.tr/ .`);
 text(sl,'RING',60,174,380,120,93,C.coral,true);label(sl,'Until you stop it',60,305,390,C.white,33);
 para(sl,'Answer / reject\nor press Stop alert',60,368,395,29,C.muted,100);
 para(sl,'No auto-answer\nNo after-answer audio',60,500,410,28,C.teal,95);
 const stations=[['701','Matryoshka Radio'],['702','Echo online'],['703','RADIO BOB! Rock'],['704','RADIO BOB! Metal'],['705','RADIO BOB! Gothic'],['706','SomaFM PopTron'],['707','Radyo Viva / Turkish']];
 stations.forEach((a,i)=>{const y=173+i*60;text(sl,a[0],530,y,110,45,30,i%2?C.yellow:C.teal,true);text(sl,a[1],678,y+3,535,43,28,C.white);});
 foot(sl,'For 708–710: handset down, speaker off, enter all digits, then press Dial.');
}
// 18: Account setup, SIP registration and usable media are different milestones.
{
 const sl=base('Free SIP account status', `Status snapshot: October 17, 2026. SIP2SIP, Callcentric and the Linphone SIP provider account all reached Registered in the live Asterisk instance. That external Linphone account is separate from the Mac client on extension 209. Callcentric IP Freedom has a $0 plan and $0 balance, but outbound calls require provider risk review. Callcentric remains registration-only with 801 withheld. The deployed Linphone profile uses the independently published The Test Call peer, sip:thetestcall@sip.linphone.org, for 803. A bounded live pilot answered and exchanged RTP in both directions: transmitted packets increased from 252 to 503 and received packets from 222 to 429, while locally reported receive loss stayed at zero in both samples. The latest bounded SIP2SIP 802 pilot to fixed echo destination 4444 also answered, with transmitted packets increasing from 260 to 510 and received packets from 214 to 416. Its local receive-loss counter increased from 44 to 91. These samples establish answered calls and packet exchange, not an exact overall loss percentage, cause, voice quality or working DTMF menu. Both tests ended with zero active channels after exact cleanup. Human listening and DTMF menu interaction remain untested. Free accounts do not imply free PSTN. FWD stopped at its mandatory Verification Fee checkout. Google Voice supplies no official free generic Asterisk SIP trunk. Sources: https://sip2sip.info/help/ ; https://www.callcentric.com/rate/plans/ip_freedom/ ; https://www.callcentric.com/support/device/asterisk/17_pjsip ; https://www.linphone.org/en/faq/ ; https://thetestcall.blogspot.com/ ; https://www.fwdialup.com/mission ; https://knowledge.workspace.google.com/admin/voice/sip-link-requirements ; docs/server-validation.md and docs/external-sip.md .`);
 const rows=[['SIP2SIP','Echo answered, RTP loss observed',C.yellow],['Callcentric','Registered, outbound review required',C.yellow],['Linphone SIP','803 answered, two-way RTP observed',C.teal],['FWD','Stopped at mandatory Verification Fee',C.coral],['Google Voice','No official free Asterisk SIP trunk',C.muted]];
 rows.forEach((a,i)=>{const y=170+i*79;text(sl,a[0],60,y,335,48,30,a[2],true);text(sl,a[1],435,y+2,790,57,27,C.white);});
 foot(sl,'Answered calls and RTP verified. Human listening and DTMF menu tests remain pending.');
}
// 19: Registration, decoder output and listener confirmation are distinct results.
{
 const sl=base('Mac calling and children’s radio', `Extension 209 has a dedicated TCP/IPv4 account on the home PBX. Zoiper5 is the current compact Mac client, replacing the earlier Telephone and Linphone trials. Zoiper registration and an echo call to 600 passed, and the user confirmed audible echo. Earlier Linphone 6.2.3 tests reached Up/Echo with PCMU and increasing RTP in both directions; those are historical tests of the same independent PBX account. Allow registration to recover after a PBX restart before retrying a call. The optional site.softphones entry has its own preserved secret and an exact source IPv4 ACL. It creates no Cisco XML, does not join ring group 620 and is not a Cisco gateway device. The optional incoming group 621 can include this client through explicit private membership. The three Deti FM channels are 708 toddler music (channel 327), 709 lullabies (channel 95), and 710 children's songs (channel 93). All three passed bounded stream decoding in the PBX namespace and produced PCM through the installed worker. A listener confirmed music on 708; this does not establish human-audible playback for every station. With the Cisco handset down and speaker/headset off, type the full station number then press Dial, without first pressing a line key. No continuous download is required. Sources: https://www.zoiper.com/en/voip-softphone/download/current ; https://detifm.ru/channel/327 ; https://detifm.ru/channel/95 ; https://detifm.ru/channel/93 ; docs/macos-softphone.md and docs/radio-and-alarms.md .`);
 text(sl,'209',60,175,410,140,110,C.teal,true);
 label(sl,'Zoiper5 · Mac 209',60,335,440,C.white,30);
 para(sl,'Own SIP identity\nInternal calls and services\nExcluded from ring group 620',60,407,465,26,C.muted,135);
 const additions=[['708','Toddler music',C.coral],['709','Lullabies',C.yellow],['710','Children’s songs',C.teal]];
 additions.forEach((a,i)=>{const y=182+i*121;text(sl,a[0],650,y,145,65,45,a[2],true);text(sl,a[1],820,y+7,390,60,29,C.white,true);});
 para(sl,'Official Deti FM channels',650,554,555,24,C.muted,45);
 foot(sl,'Mac 209 echo: RTP and human listening passed. All three stations are deployed; 708 was heard.');
}
const candidate=path.join(build,'candidate.pptx');await (await PresentationFile.exportPptx(p)).save(candidate);
const final=path.join(out,'cisco-home-telephony.pptx');
if(process.env.PRESENTATION_SKILL_DIR){
 const sd=process.env.PRESENTATION_SKILL_DIR;const {finalizePresentation}=await import(pathToFileURL(path.join(sd,'container_tools/artifact_tool_utils.mjs')).href);const validatedDir=path.join(build,'finalized'); await fs.mkdir(validatedDir,{recursive:true}); const validated=path.join(validatedDir,`validated-${Date.now()}.pptx`);
 const result=await finalizePresentation({workspaceDir:root,candidatePath:candidate,finalPath:validated,pythonExecutable:process.env.RUNTIME_PYTHON??'python3',integrityValidatorPath:path.join(sd,'container_tools/inspect_presentation_package_integrity.py'),layoutValidatorPath:path.join(sd,'container_tools/inspect_presentation_layout_geometry.py'),layoutArgs:['--expected-slide-size-emu','12192000,6858000','--validate-bullet-geometry','--validate-heading-fit'],explicitTotalSlideCount:p.slides.items.length,requiredNativeTableOwnerSlides:[],requiredNativeChartOwnerSlides:[],fontPolicy:{basis:'design',families:[FONT]},verifyArtifactToolImport:true,receiptPath:path.join(build,`validation-${Date.now()}.json`)});
 await fs.copyFile(validated,final);console.log('PPTX structural validation and import passed.');
}else await fs.copyFile(candidate,final);
for(const [i,sl] of p.slides.items.entries())await bytes(path.join(build,`slide-${String(i+1).padStart(2,'0')}.png`),await p.export({slide:sl,format:'png',scale:1}));
await bytes(path.join(build,'montage.webp'),await p.export({format:'webp',montage:true,scale:.5}));
await fs.copyFile(path.join(build,'montage.webp'),path.join(out,'preview.webp'));
try {
 await bytes(path.join(out,'cisco-home-telephony.pdf'),await p.export({format:'pdf'}));
} catch (err) {
 const pdfDir=await fs.mkdtemp(path.join(build,'pdf-'));
 const profile=pathToFileURL(path.join(pdfDir,'lo-profile')).href;
 try {
  await promisify(execFile)(process.env.SOFFICE??'soffice', ['-env:UserInstallation='+profile,'--headless','--convert-to','pdf','--outdir',pdfDir,final]);
  await fs.copyFile(path.join(pdfDir,'cisco-home-telephony.pdf'),path.join(out,'cisco-home-telephony.pdf'));
 } catch (pdfErr) { throw new Error('PPTX created. PDF export needs a working LibreOffice: set SOFFICE to its executable.',{cause:pdfErr}); }
}
await fs.writeFile(path.join(build,'content.ndjson'),(await p.inspect({kind:'slide,textbox,shape,notes',maxChars:2000000})).ndjson);
console.log(`Created ${p.slides.items.length}-slide PPTX + PDF and 4 editable SVG diagrams.`);
