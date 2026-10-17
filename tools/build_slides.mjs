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
 'architecture':{title:'Cisco, Asterisk и Home Assistant',description:'Телефоны питаются через PoE и звонят через Asterisk. Home Assistant подключается к AMI, а Assist принимает SIP/RTP от Asterisk. Существующий DHCP остаётся, TFTP включается временно.',
 nodes:[rect('phones',45,185,265,120,'10 × Cisco 7945G\nPoE 802.3af'),rect('switch',455,185,290,120,'PoE-коммутатор\nVoice LAN / VLAN'),rect('ast',900,185,330,120,'Asterisk / PJSIP\nфиксированный IP'),rect('router',455,440,290,110,'Маршрутизатор\nDHCP option 150',C.yellow),rect('tftp',45,440,265,110,'TFTP временно\nпрошивка и XML',C.coral),rect('ha',900,440,330,110,'Home Assistant\nAMI + VoIP / Assist',C.yellow)],
 edges:[edge('phones','switch','Ethernet',C.teal,'right','left',true),edge('switch','ast','SIP / RTP',C.teal,'right','left',true),edge('router','switch','DHCP',C.yellow,'top','bottom',false,[665,382]),edge('tftp','phones','загрузка',C.coral,'top','bottom',false,[235,382]),edge('ast','ha','AMI\nSIP / RTP',C.yellow,'bottom','top',true,[1160,360])],
 footer:'Asterisk: hostNetwork и одна реплика. HA открывает AMI-соединение к PBX.\nTFTP доступен только при provisioning. SIP, RTP и AMI остаются в LAN / VPN.'},
 'assist':{title:'630: голосовой помощник в телефоне',description:'Asterisk преобразует G.711 Cisco в Opus для встроенной VoIP интеграции Home Assistant. AMI не передаёт голос. Реальный звук и распознавание проверяют на пилотном телефоне.',
 nodes:[rect('cisco',45,210,280,140,'Cisco 7945G\nномер 201\nG.711'),rect('codec',480,210,315,140,'Asterisk\nG.711 ↔ Opus\nномер 630',C.coral),rect('assist',950,210,285,140,'Home Assistant\nVoIP / Assist\nOpus',C.yellow)],
 edges:[edge('cisco','codec','SIP TCP\nRTP',C.teal,'right','left',true,[402,180]),edge('codec','assist','SIP UDP\nRTP',C.yellow,'right','left',true,[873,180])],
 annotations:[{x:45,y:435,w:1190,h:85,text:'Первый вызов: Allow calls и выбор pipeline в HA.\nЗатем проверка распознавания, ответа TTS и завершения.',size:28,color:C.white}],
 footer:'SIP From сохраняет номер телефона. На HA RTP / RTCP используют динамические UDP-порты.\nНастоящий разговор подтверждает пилот с телефоном, а не только readiness Pod.'},
 'commissioning':{title:'Один телефон до всей партии',description:'Обновление проходит ступенчато, если это требуется исходной прошивкой. Загрузка подтверждается на экране телефона.',
 nodes:[rect('inventory',45,195,290,120,'Инвентарь\nPID / HW / Load',C.yellow),rect('firmware',480,195,290,120,'Цепочка версий\nSCCP → SIP',C.coral),rect('tftp',915,195,290,120,'TFTP готов\nпакет + SEP XML'),rect('pilot',915,430,290,120,'Один телефон\nстабильное питание'),rect('accept',480,430,290,120,'Тесты звонков\nзвук / DTMF / reboot'),rect('fleet',45,430,290,120,'Вся партия\nпо одному',C.yellow)],
 edges:[edge('inventory','firmware'),edge('firmware','tftp'),edge('tftp','pilot','',C.teal,'bottom','top'),edge('pilot','accept','',C.teal,'left','right'),edge('accept','fleet','',C.yellow,'left','right')],
 footer:'HW 13.0+: минимум 9.3(1)SR3. Цепочка обновления зависит от текущей версии.\nДо сброса готовы TFTP и recovery-план. Питание при записи flash не прерывать.'},
 'power':{title:'7945G поддерживает PoE без переделки',description:'802.3af уже встроен в телефон. При отсутствии PoE у коммутатора добавьте активный IEEE 802.3af-инжектор.',
 nodes:[rect('poe',60,185,300,120,'PoE-коммутатор\nIEEE 802.3af'),rect('phone1',900,185,310,120,'Cisco 7945G\nпорт SW / Network'),rect('lan',60,440,300,115,'Обычный коммутатор\nEthernet',C.yellow),rect('injector',480,440,300,115,'Активный инжектор\nIEEE 802.3af',C.coral),rect('phone2',900,440,310,115,'Cisco 7945G\nпорт SW / Network')],
 edges:[edge('poe','phone1','данные + питание'),edge('lan','injector','данные',C.yellow),edge('injector','phone2','PoE')],
 footer:'Пассивный 24 V инжектор не подходит. UPS питает также PoE-коммутатор, маршрутизатор и k3s-узел.'}
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
 const sl=base('', 'Партия: 10 × Cisco 7945G. Модель подтверждена письмом eBay о доставке от 1 декабря 2022 года. Адрес доставки и платёжные данные в репозиторий не включены. Состояние и прошивка каждого сохранившегося аппарата требуют инвентаризации. Обновлено: 17 октября 2026 года. Слайды описывают план и архитектуру, а не подтверждают установку в доме.');
 text(sl,'Cisco 7945G',60,135,1160,115,80,C.teal,true);text(sl,'Домашняя телефония',60,265,1160,105,61,C.white,true);text(sl,'Asterisk в k3s + Home Assistant',60,400,1100,75,41,C.coral);text(sl,'10 аппаратов с цветным экраном',60,540,1100,60,29,C.muted);}
// 02: Inventory facts, not a generic product comparison.
{
 const sl=base('Наша партия: 10 × Cisco 7945G',`Модель и количество подтверждены письмом eBay о доставке 01.12.2022. Cisco firmware family 7945_7965: ${S.firmware}\nPoE: ${S.power}\nПоддержка SIP подтверждает наличие прошивки. Совместимость функций с Asterisk проверяется в пилоте.`);
 text(sl,'10',65,185,250,190,132,C.teal,true);text(sl,'телефонов',70,380,250,55,28,C.muted);
 label(sl,'SIP-прошивка семейства 7945_7965',385,180,810,C.white,33);para(sl,'Если сейчас SCCP, нужен переход на SIP.\nMPP-конверсия этой модели не требуется.',385,290,805,27,C.muted,110);
 label(sl,'Перед работой',385,465,790,C.yellow,28);para(sl,'С каждого телефона: PID, VID/HW, MAC,\nApp Load, Boot Load, IP и TFTP.',385,520,805,27,C.white,100);
}
// 03: Architecture.
diagramSlide('architecture',`Проектное решение: одна реплика Asterisk на закреплённом LAN-узле, hostNetwork, постоянный LAN IP. Существующий router DHCP сохраняется. TFTP включается по необходимости. HA открывает TCP AMI к Asterisk. Для Assist Asterisk открывает SIP к HA VoIP, голос идёт в обе стороны. Home Assistant может работать на отдельном LAN-узле или через существующий VPN, HTTP Ingress не передаёт эти протоколы. Kubernetes: ${S.kube}\nPJSIP: ${S.pjsip}\nHA: ${S.ha}\nVoIP: ${S.voip}\nСм. docs/k3s-deployment.md и docs/home-assistant.md.`);
// 04: Ports as flat typographic rows.
{
 const sl=base('Сеть для сигнализации и голоса',`Проектные порты на PBX: SIP TCP5060 по умолчанию, UDP5060 opt-in, RTP UDP10000–10199. Для tftpd-hpa UDP69 и UDP20000–20031. AMI TCP5038 только от фактического HA IP/32. HA VoIP слушает SIP UDP5060 либо выбранный порт и динамические RTP/RTCP UDP-порты, доступные только от PBX. Cisco RTP: UDP16384–16482. Firewall должен учитывать обе стороны потока. NetworkPolicy не является единственной защитой hostNetwork. PJSIP: ${S.pjsip}\nVoIP: ${S.voip}\nСм. docs/home-assistant.md.`);
 const rows=[['5060','TCP / UDP','SIP: Cisco TCP, HA UDP',C.teal],['10000–10199','UDP на PBX','RTP / голос',C.teal],['5038','TCP на PBX','AMI, только HA IP /32',C.yellow],['69 + 20000–20031','UDP на PBX','TFTP / временно',C.coral]];
 rows.forEach((r,i)=>{const y=170+i*102;text(sl,r[0],60,y,490,60,r[0].length>11?33:41,r[3],true);text(sl,r[1],570,y+7,225,45,26,C.white);text(sl,r[2],805,y+7,420,60,25,C.muted);});
 foot(sl,'На HA RTP / RTCP динамические. Разрешения firewall ограничивают нужными IP.');
}
// 05: Native editable power diagram.
diagramSlide('power',`Cisco 7945G поддерживает IEEE 802.3af: ${S.power}\nЕсли существующий коммутатор без PoE, нужен активный IEEE 802.3af injector или PoE switch, аппаратная переделка телефона не требуется. Подключать в сетевой порт SW/Network. Проверять бюджет порта и суммарный бюджет коммутатора. Для питания при отключении электроэнергии нужен UPS всей цепочке.`);
// 06: Native editable conversion flow.
diagramSlide('commissioning',`Универсальной команды factory reset для всех состояний нет. Требуется проверить текущую App Load и цепочку обновления. Cisco README для семейства 7945_7965: ${S.firmware}\nДля HW13.0+ запрещён downgrade ниже 9.3(1)SR3. Нельзя применять промежуточную 8.5(2) ко всей партии. Для исходной версии до 8.3(3) при переходе к 9.3(1)+ требуется промежуточный этап 8.3(3)–8.5(2), с дополнительными оговорками в release notes конкретной версии: ${S.upgrade}\nПакет Cisco получают легально, SHA-256 фиксируют локально. Одного .loads недостаточно.`);
// 07: TFTP specifics.
{
 const sl=base('TFTP до первого перезапуска',`Cisco: ${S.firmware}\nТочное имя loadInformation выбирается из README и .loads выбранного пакета. Нельзя переносить файл от 7942/7962. SEP filenames используют MAC без разделителей и верхний регистр. Конфигурация может содержать SIP-пароль, поэтому TFTP доступен только доверенной LAN. DHCP option 150 задают на существующем DHCP-сервере либо используют Alternate TFTP телефона.`);
 label(sl,'Полный официальный пакет',60,180,1100,C.teal,34);para(sl,'.loads и все связанные бинарные файлы\nс оригинальными именами и регистром',60,245,1100,30,C.white,95);
 label(sl,'SEP<MAC>.cnf.xml',60,390,1100,C.coral,34);para(sl,'SIP-сервер, внутренний номер и отдельный пароль\nLoad ID только для выбранной ветки 7945_7965',60,454,1100,29,C.white,105);
 foot(sl,'DHCP option 150 на домашнем маршрутизаторе или Alternate TFTP на телефоне.');
}
// 08: Local SIP config.
{
 const sl=base('Asterisk: свой номер для каждого телефона',`PJSIP examples: ${S.pjsip}\nПроектный baseline: endpoint/auth/aor на каждый аппарат, уникальный пароль, transport согласован с phone XML. TCP по умолчанию, UDP для телефонов включают явно. Кодеки G.711 ulaw/alaw, direct_media=no удерживает RTP через Asterisk. NAT-настройки не добавляют без необходимости в плоской LAN. Стартовый сценарий ограничен внутренними звонками без PSTN/SIP trunk. Тестовые номера 600 echo, 601 tone, 620 ring group, 630 Assist при включённой HA-интеграции.`);
 text(sl,'201',60,195,310,100,74,C.teal,true);text(sl,'202',470,195,310,100,74,C.coral,true);text(sl,'…210',880,195,335,100,74,C.yellow,true);
 para(sl,'TCP, отдельные endpoint, auth и AOR\nУникальный пароль на каждом аппарате',60,355,1100,31,C.white,100);
 para(sl,'G.711 ulaw / alaw\ndirect_media=no для RTP через Asterisk',60,495,1100,29,C.muted,100);
 foot(sl,'600 echo, 601 tone, 620 группа. Номер 630 доступен при включённом Assist.');
}
// 09: AMI monitoring and call control are separate from voice transport.
{
 const sl=base('AMI: состояния и управление вызовами',`Установка и пакет HA: ${S.ha}\nПроверенная TECH7Fox Asterisk integration v1.0.5, commit: ${S.upstream}\nHA открывает обычное TCP5038 к PBX, без TLS. Разрешён только фактический адрес источника /32 через LAN или VPN. Отдельные секреты .local/ha-ami-secret.json не входят в Git и phone XML. read=call,dtmf; write=system плюс originate только при allow_originate:true. system нужен upstream для discovery, но также разрешает Hangup и другие административные действия, поэтому это не read-only доступ. Один глобальный service asterisk.send_action на HA, без выбора нескольких PBX. DTMF timestamp-state и атрибут Digit; направления Sent/Received относятся к Asterisk. Registered вычисляется из DeviceState, не заменяет pjsip show contacts. Нужны реальные entity IDs и проверка на двух аппаратах.`);
 label(sl,'Home Assistant подключается к PBX:5038',60,175,1110,C.teal,34);
 para(sl,'Статус, связанная линия и DTMF\nФактические entity IDs из вашего HA',60,238,1110,29,C.white,100);
 label(sl,'Вызов и завершение конкретного канала',60,385,1110,C.yellow,33);
 para(sl,'asterisk.send_action: Originate / Hangup\nDTMF-пример отправляет безопасное уведомление',60,451,1110,28,C.white,110);
 foot(sl,'Отдельный секрет и ACL /32. Право system даёт административные действия.');
}
// 10: Native editable voice path, also exported as standalone SVG.
diagramSlide('assist',`Официальная VoIP-интеграция HA: ${S.voip}\nУстановка и приёмка: ${S.ha}\nЗакреплённый Opus transcoder: ${S.opus}\nCisco 7945G использует G.711, проверенная HA 2026.10.2 VoIP требует Opus. Asterisk транскодирует звук в обе стороны через модуль, собранный из исходников Wazo. Проверки translation paths и контейнерные smoke подтверждают серверную часть, не разговор с домашним аппаратом. SIP к Cisco по умолчанию TCP5060, к HA UDP5060 либо явно выбранный порт. PBX RTP10000–10199, у HA динамические RTP/RTCP UDP-порты. Allow calls и выбор pipeline включают для обнаруженного VoIP device. SIP From сохраняет внутренний номер, в том числе при Originate из HA. Проверить STT, ответ TTS, завершение и повторный вызов. AMI передаёт события и команды, голос не передаёт.`);
// 11: Deployment stages do not claim a completed household installation.
{
 const sl=base('Этапы развёртывания и подключения',`См. README.md, docs/pbx-bootstrap.md, docs/k3s-deployment.md, docs/home-assistant.md, scripts/ и deploy/. namespace: telephony. Bootstrap означает подготовку серверной части, образов и сетевых правил до подключения проверенного телефона, а не завершённый звонок. Для него существует явный provision.py --pbx-only, без фиктивных MAC, телефонных endpoint и TFTP. Начальные allow_originate:false и assist.enabled:false сохраняются до проверки условий подключения. Локальные site/inventory и секреты не коммитятся. Перед TFTP нужен фактический инвентарь аппарата и подходящий проверенный firmware. Точные команды генерации и применения брать из актуального runbook. Одна реплика Asterisk и Recreate предотвращают конфликт hostNetwork-портов. HA-компонент сначала staging, затем отдельное окно установки в существующий HA. k3s: ${S.k3s}\nHA: ${S.ha}`);
 const steps=[['01','Bootstrap сервера','Образы, постоянный IP, firewall, Asterisk и Opus'],['02','Проверенный телефон','PID / HW / Load / MAC, SIP-профиль, временный TFTP'],['03','Подключение Home Assistant','AMI /32, секрет, реальные entity IDs и VoIP pipeline'],['04','Приёмка и расширение','Два аппарата, вызов 630, затем остальные телефоны']];
 steps.forEach((a,i)=>{let y=165+i*111;text(sl,a[0],60,y,80,60,40,[C.teal,C.coral,C.yellow,C.teal][i],true);text(sl,a[1],180,y,1000,48,32,C.white,true);text(sl,a[2],180,y+49,1000,45,25,C.muted);});
}
// 12: Physical phone acceptance remains explicit.
{
 const sl=base('Приёмка с реальными аппаратами',`Критерии проекта, а не заявление о завершённой установке в доме. Нужны два физических аппарата с проверенным инвентарём. Проверить регистрацию pjsip show contacts, вызовы 201→202 и 202→201, звук в обе стороны, DTMF/echo, hold/transfer и время. Затем состояния HA, Originate, Hangup фактического активного Channel и безопасный DTMF notification. DTMF Sent/Received определить на пилоте. Номер 630: Allow calls, pipeline, корректная отдельная идентичность каждого телефона, STT, TTS и завершение. Повторить после cold boot телефона и контролируемого restart PBX/HA. Автоматические runtime/smoke проверки не заменяют физическую приёмку. ${S.ha}/blob/main/docs/acceptance.md`);
 const steps=[['Телефония','201 и 202: регистрация, двусторонний звук, hold / transfer',C.teal],['Home Assistant','Состояния, Originate, Hangup нужного Channel, DTMF',C.yellow],['Assist 630','Распознавание, ответ голосом и завершение разговора',C.coral]];
 steps.forEach((a,i)=>{const y=177+i*139;label(sl,a[0],60,y,1110,a[2],34);para(sl,a[1],60,y+61,1110,27,C.white,63);});
 foot(sl,'Повторить после перезапусков телефона, PBX и HA. Readiness не подтверждает звук.');
}
// 13: Operations.
{
 const sl=base('Эксплуатация и восстановление',`Проектные эксплуатационные требования. Поддерживается один узел для телефонии, это не высокая доступность. Backup включает inventory, SIP/AMI секреты, конфиги, persistent Asterisk data, provenance firmware, HA config/.storage и entity mapping. Проверять restore отдельно. Не публиковать SIP/AMI пароли и firmware binaries. В HA исключить сырые DTMF/call sensors из Recorder, исключения не удаляют старую историю. Откат home_assistant.enabled:false отключает AMI и маршрут630 после генерации и применения, базовые внутренние звонки остаются. См. docs/operations.md и ${S.ha}/blob/main/docs/privacy.md`);
 label(sl,'Резервная копия вне Git',60,185,1110,C.teal,34);para(sl,'Инвентарь, SIP / AMI секреты и данные Asterisk\nHA config / .storage, entity mapping, версии образов',60,250,1110,29,C.white,100);
 label(sl,'Один узел означает одну точку отказа',60,410,1110,C.coral,34);para(sl,'UPS для сетевого оборудования и k3s-узла\nПлан восстановления с тем же LAN IP',60,475,1110,29,C.white,100);
 foot(sl,'Raw DTMF и связанные линии исключают из Recorder. Старую историю проверяют отдельно.');
}
// 14: End with an explicit distinction between preparation and acceptance.
{
 const sl=base('Готовность сервера и готовность телефонии', 'Состояние реального оборудования фиксируется отдельно в локальном журнале приёмки. Слайды не утверждают, что сервер уже установлен в доме или первый звонок завершён. Фактическое количество сохранившихся устройств, PID/HW/MAC, App Load, сетевые адреса и питание требуют инвентаризации. Успешные автоматические тесты подтверждают компоненты и конфигурацию. Физическая приёмка включает голос, HA call control, DTMF и Assist на реальных телефонах. После пилота подключать оставшуюся партию по одному.');
 label(sl,'Сервер можно подготовить первым',60,180,1110,C.teal,36);
 para(sl,'Образы, конфигурация, AMI и Opus\nПроверки подтверждают серверные компоненты',60,248,1110,29,C.white,100);
 label(sl,'Телефонию подтверждает пилот',60,398,1110,C.coral,36);
 para(sl,'Инвентарь аппарата, звонок двух участников и Assist\nПосле приёмки подключаются остальные телефоны',60,466,1110,29,C.white,100);
 foot(sl,'Физическая приёмка остаётся отдельным этапом. Результаты заносятся в локальный журнал.');
}
const candidate=path.join(build,'candidate.pptx');await (await PresentationFile.exportPptx(p)).save(candidate);
const final=path.join(out,'cisco-home-telephony.pptx');
if(process.env.PRESENTATION_SKILL_DIR){
 const sd=process.env.PRESENTATION_SKILL_DIR;const {finalizePresentation}=await import(pathToFileURL(path.join(sd,'container_tools/artifact_tool_utils.mjs')).href);const validatedDir=path.join(build,'finalized'); await fs.mkdir(validatedDir,{recursive:true}); const validated=path.join(validatedDir,`validated-${Date.now()}.pptx`);
 const result=await finalizePresentation({workspaceDir:root,candidatePath:candidate,finalPath:validated,pythonExecutable:process.env.RUNTIME_PYTHON??'python3',integrityValidatorPath:path.join(sd,'container_tools/inspect_presentation_package_integrity.py'),layoutValidatorPath:path.join(sd,'container_tools/inspect_presentation_layout_geometry.py'),layoutArgs:['--expected-slide-size-emu','12192000,6858000','--validate-bullet-geometry','--validate-heading-fit'],explicitTotalSlideCount:14,requiredNativeTableOwnerSlides:[],requiredNativeChartOwnerSlides:[],fontPolicy:{basis:'design',families:[FONT]},verifyArtifactToolImport:true,receiptPath:path.join(build,`validation-${Date.now()}.json`)});
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
console.log('Created 14-slide PPTX + PDF and 4 editable SVG diagrams.');
