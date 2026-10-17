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
 kube:'https://kubernetes.io/docs/concepts/workloads/controllers/deployment/'
};
await Promise.all([out,diagramOut,build].map(p=>fs.mkdir(p,{recursive:true})));
const rect=(id,x,y,w,h,label,accent=C.teal)=>({id,x,y,w,h,label,accent});
const edge=(from,to,label='',color=C.teal,fromSide='right',toSide='left')=>({from,to,label,color,fromSide,toSide});
const diagrams={
 'architecture':{title:'Cisco 7945G в домашней сети',description:'Телефоны подключаются к одному LAN-адресу k3s-узла. Существующий маршрутизатор раздаёт DHCP. TFTP работает только при provisioning.',
 nodes:[rect('phones',50,290,245,120,'10 × Cisco 7945G\nPoE 802.3af'),rect('switch',350,290,265,120,'Коммутатор PoE\nVoice LAN / VLAN'),rect('router',350,120,265,100,'Маршрутизатор\nDHCP option 150',C.yellow),rect('ast',830,235,365,130,'Asterisk / PJSIP\nSIP TCP+UDP 5060\nRTP UDP 10000–10199'),rect('tftp',830,445,365,105,'TFTP временно\nUDP 69 + 20000–20031',C.coral)],
 edges:[edge('phones','switch'),edge('router','switch','DHCP',C.yellow,'bottom','top'),edge('switch','ast','SIP + RTP'),edge('switch','tftp','загрузка',C.coral)],
 extras:[{x:820,y:180,w:390,h:390,label:'k3s: фиксированный LAN-узел',color:C.muted}], footer:'hostNetwork · одна реплика Asterisk · постоянный LAN IP · без Ingress для SIP/RTP'},
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
function diagramSvg(d){let s=`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" role="img" aria-labelledby="title desc"><title id="title">${esc(d.title)}</title><desc id="desc">${esc(d.description)}</desc><defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto-start-reverse"><path d="M0,0 L8,4 L0,8" fill="context-stroke"/></marker></defs><rect width="1280" height="720" fill="${C.navy}"/>${svgText(d.title,45,55,34,C.white,'start',700)}`;
 for(const e of d.extras??[])s+=`<rect x="${e.x}" y="${e.y}" width="${e.w}" height="${e.h}" rx="12" fill="none" stroke="${e.color}" stroke-dasharray="7 7"/>${svgText(e.label,e.x+16,e.y+32,20,e.color)}`;
 for(const e of d.edges){const a=port(d.nodes.find(n=>n.id===e.from),e.fromSide),b=port(d.nodes.find(n=>n.id===e.to),e.toSide),mid=(a[0]+b[0])/2;const dpath=a[1]===b[1]||a[0]===b[0]?`M${a} L${b}`:`M${a} H${mid} V${b[1]} H${b[0]}`;s+=`<path d="${dpath}" fill="none" stroke="${e.color}" stroke-width="3" marker-end="url(#arrow)"/>`;if(e.label){const lp=d===diagrams.architecture?(e.from==='router'?[540,260]:e.to==='ast'?[735,263]:[760,425]):[mid,(a[1]+b[1])/2-16];s+=svgText(e.label,lp[0],lp[1],19,e.color,'middle');}}
 for(const n of d.nodes)s+=`<rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="10" fill="${C.ink}" stroke="${n.accent}" stroke-width="2"/>${svgText(n.label,n.x+n.w/2,n.y+n.h/2-(n.label.split('\n').length-1)*16+8,24,C.white,'middle',600)}`;
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
 for(const e of d.edges){sl.shapes.connect(shapes.get(e.from),shapes.get(e.to),{kind: (['power','commissioning'].includes(name)?'straight':'elbow'),fromSide:e.fromSide,toSide:e.toSide,line:{fill:e.color,width:3},tail:{type:'triangle',width:'sm',length:'sm'}});if(e.label){const a=port(d.nodes.find(n=>n.id===e.from),e.fromSide),b=port(d.nodes.find(n=>n.id===e.to),e.toSide);const lp=name==='architecture'?(e.from==='router'?[518,244,100]:e.to==='ast'?[644,263,170]:[722,390,150]):[(a[0]+b[0])/2*sc+dx-108,(a[1]+b[1])/2*sc+dy-35,216];text(sl,e.label,lp[0],lp[1],lp[2],32,18,e.color,false,'center');}}
 foot(sl,d.footer.replaceAll(' · ','  /  '));return sl;}
// 01: Minimal title, editable typography.
{
 const sl=base('', 'Партия: 10 × Cisco 7945G. Модель подтверждена письмом eBay о доставке от 1 декабря 2022 года. Адрес доставки и платёжные данные в репозиторий не включены. Состояние и прошивка каждого сохранившегося аппарата требуют инвентаризации. Дата подготовки: 16 октября 2026 года.');
 text(sl,'Cisco 7945G',60,135,1160,115,80,C.teal,true);text(sl,'Домашняя телефония',60,265,1160,105,61,C.white,true);text(sl,'SIP и Asterisk в k3s',60,400,1100,75,41,C.coral);text(sl,'10 аппаратов с цветным экраном',60,540,1100,60,29,C.muted);}
// 02: Inventory facts, not a generic product comparison.
{
 const sl=base('Наша партия: 10 × Cisco 7945G',`Модель и количество подтверждены письмом eBay о доставке 01.12.2022. Cisco firmware family 7945_7965: ${S.firmware}\nPoE: ${S.power}\nПоддержка SIP подтверждает наличие прошивки. Совместимость функций с Asterisk проверяется в пилоте.`);
 text(sl,'10',65,185,250,190,132,C.teal,true);text(sl,'телефонов',70,380,250,55,28,C.muted);
 label(sl,'SIP-прошивка семейства 7945_7965',385,180,810,C.white,33);para(sl,'Если сейчас SCCP, нужен переход на SIP.\nMPP-конверсия этой модели не требуется.',385,290,805,27,C.muted,110);
 label(sl,'Перед работой',385,465,790,C.yellow,28);para(sl,'С каждого телефона: PID, VID/HW, MAC,\nApp Load, Boot Load, IP и TFTP.',385,520,805,27,C.white,100);
}
// 03: Architecture.
diagramSlide('architecture',`Проектное решение: одна реплика Asterisk на закреплённом LAN-узле, hostNetwork, постоянный LAN IP. Router DHCP сохраняется существующим. TFTP включается по необходимости. RTP-диапазон является локально выбранным, не универсальным стандартом. Kubernetes: ${S.kube}\nPJSIP: ${S.pjsip}\nСм. docs/k3s-deployment.md и deployment manifests.`);
// 04: Ports as flat typographic rows.
{
 const sl=base('Сеть для сигнализации и голоса',`Проектная конфигурация портов: SIP TCP и UDP 5060, RTP UDP 10000–10199. Для tftpd-hpa резервируется UDP 69 и диапазон передачи UDP 20000–20031. Разрешить доступ только нужной LAN/VLAN через host firewall: NetworkPolicy не является единственной защитой для hostNetwork. Для SIP/RTP HTTP Ingress не используется. ${S.pjsip}`);
 const rows=[['5060','TCP + UDP','SIP / регистрация',C.teal],['10000–10199','UDP','RTP / голос',C.teal],['69 + 20000–20031','UDP','TFTP / временно',C.coral]];
 rows.forEach((r,i)=>{const y=180+i*130;text(sl,r[0],60,y,495,60,r[0].length>11?36:43,r[3],true);text(sl,r[1],580,y+7,210,45,28,C.white);text(sl,r[2],805,y+7,410,60,28,C.muted);});
 foot(sl,'Host firewall разрешает телефонную LAN/VLAN. SIP и TFTP не публикуются в интернет.');
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
 const sl=base('Asterisk: свой номер для каждого телефона',`PJSIP examples: ${S.pjsip}\nПроектный baseline: endpoint/auth/aor на каждый аппарат, уникальный пароль, transport согласован с phone XML. TCP+UDP слушатели поддерживают проверку аппаратов. Кодеки G.711 ulaw/alaw, direct_media=no удерживает RTP через Asterisk. NAT-настройки не добавляют без необходимости в плоской LAN. Стартовый сценарий ограничен внутренними звонками без PSTN/SIP trunk.`);
 text(sl,'201',60,195,310,100,74,C.teal,true);text(sl,'202',470,195,310,100,74,C.coral,true);text(sl,'…210',880,195,335,100,74,C.yellow,true);
 para(sl,'TCP, отдельные endpoint, auth и AOR\nУникальный пароль на каждом аппарате',60,355,1100,31,C.white,100);
 para(sl,'G.711 ulaw / alaw\ndirect_media=no для RTP через Asterisk',60,495,1100,29,C.muted,100);
 foot(sl,'Первый этап: внутренние звонки в доме. Внешний SIP trunk настраивается отдельно.');
}
// 09: Deployment phases, names synchronized to runbook before final handoff.
{
 const sl=base('Развёртывание в домашнем k3s',`См. README.md, docs/k3s-deployment.md, scripts/ и deploy/. namespace: telephony. Команды: python3 scripts/provision.py --site .local/site.json --inventory .local/phones.json; python3 scripts/verify_firmware.py --directory .local/firmware --manifest .local/firmware-manifest.json; scripts/build-images.sh; scripts/deploy.sh (dry-run по умолчанию), scripts/deploy.sh --apply (явное применение). Здесь этапы, а точные команды находятся в runbook. k3s requirements: ${S.k3s}\nЗакрепление узла предотвращает незаметную смену LAN IP телефонии. Одна реплика и Recreate предотвращают одновременную попытку занять hostNetwork-порты. Private registry и imagePullSecret при необходимости.`);
 const steps=[['01','LAN IP + k3s-узел','Фиксированный LAN-узел, проверка свободных портов'],['02','Локальный render','Инвентарь, конфиги, секреты и image digest'],['03','Asterisk','Одна реплика, постоянные данные, health check'],['04','TFTP + пилот','Временный provisioning, затем тесты звонков']];
 steps.forEach((a,i)=>{let y=165+i*111;text(sl,a[0],60,y,80,60,40,[C.teal,C.coral,C.yellow,C.teal][i],true);text(sl,a[1],180,y,1000,48,32,C.white,true);text(sl,a[2],180,y+49,1000,45,25,C.muted);});
}
// 10: Acceptance tests.
{
 const sl=base('Критерии готовности пилота',`Критерии интеграционного тестирования проекта, а не заявление о проведённых проверках реального оборудования. Нужны два телефона. Проверить регистрацию, вызовы 201→202 и 202→201, голос в обе стороны, DTMF/echo test, hold и transfer, корректное время, перезагрузку телефона и контролируемый restart pod. Симуляция или static validation не заменяют проверку на аппаратах.`);
 label(sl,'Регистрация и два направления вызова',60,180,1110,C.teal,34);para(sl,'201 вызывает 202, затем 202 вызывает 201.\nВ каждом вызове слышны оба участника.',60,245,1110,29,C.white,100);
 label(sl,'Поведение после изменений',60,400,1110,C.yellow,34);para(sl,'DTMF, удержание и перевод\nПерезапуск телефона и Asterisk, повторная регистрация',60,465,1110,29,C.white,105);
 foot(sl,'Успешный deploy сам по себе не подтверждает, что голос проходит в обе стороны.');
}
// 11: Operations.
{
 const sl=base('Эксплуатация и восстановление',`Проектные эксплуатационные требования. Поддерживается один узел для телефонии, это не высокая доступность. Резервная копия должна включать inventory, секреты, конфиги, persistent Asterisk data и provenance firmware, при необходимости voicemail. Проверять restore на отдельной тестовой среде. При смене LAN IP обновляют provisioning и сетевые правила. Не публиковать SIP-пароли и firmware binaries в Git.`);
 label(sl,'Резервная копия вне Git',60,185,1110,C.teal,34);para(sl,'Инвентарь, секреты, конфиги и данные Asterisk\nВерсия образа и происхождение firmware',60,250,1110,29,C.white,100);
 label(sl,'Один узел означает одну точку отказа',60,410,1110,C.coral,34);para(sl,'UPS для сетевого оборудования и k3s-узла\nПлан восстановления с тем же LAN IP',60,475,1110,29,C.white,100);
 foot(sl,'Секреты и бинарные прошивки хранятся локально или в закрытом хранилище.');
}
// 12: End with a concrete rollout.
{
 const sl=base('Запуск партии по шагам', 'План запуска, реальные телефоны ещё не прошиты и кластер не изменён. Начать с одного аппарата, затем проверить пару, затем подключить остальные. Фактическое количество сохранившихся устройств, App Load, адрес LAN-узла, DHCP router и питание требуют локальной инвентаризации. Репозиторий содержит инструкции, сценарии, SVG и редактируемую презентацию.');
 text(sl,'1',60,185,230,150,105,C.teal,true);text(sl,'2',470,185,230,150,105,C.coral,true);text(sl,'10',880,185,290,150,105,C.yellow,true);
 label(sl,'Один аппарат',60,370,320,C.white,29);label(sl,'Два участника',470,370,340,C.white,29);label(sl,'Вся партия',880,370,310,C.white,29);
 para(sl,'Прошивка\nи регистрация',60,442,330,28,C.muted,100);para(sl,'Двусторонний\nзвонок и DTMF',470,442,330,28,C.muted,100);para(sl,'Последовательное\nподключение',880,442,330,28,C.muted,100);
 foot(sl,'В репозитории: runbook, scripts, manifests, SVG и эта редактируемая презентация.');
}
const candidate=path.join(build,'candidate.pptx');await (await PresentationFile.exportPptx(p)).save(candidate);
const final=path.join(out,'cisco-home-telephony.pptx');
if(process.env.PRESENTATION_SKILL_DIR){
 const sd=process.env.PRESENTATION_SKILL_DIR;const {finalizePresentation}=await import(pathToFileURL(path.join(sd,'container_tools/artifact_tool_utils.mjs')).href);const validatedDir=path.join(build,'finalized'); await fs.mkdir(validatedDir,{recursive:true}); const validated=path.join(validatedDir,`validated-${Date.now()}.pptx`);
 const result=await finalizePresentation({workspaceDir:root,candidatePath:candidate,finalPath:validated,pythonExecutable:process.env.RUNTIME_PYTHON??'python3',integrityValidatorPath:path.join(sd,'container_tools/inspect_presentation_package_integrity.py'),layoutValidatorPath:path.join(sd,'container_tools/inspect_presentation_layout_geometry.py'),layoutArgs:['--expected-slide-size-emu','12192000,6858000','--validate-bullet-geometry','--validate-heading-fit'],explicitTotalSlideCount:12,requiredNativeTableOwnerSlides:[],requiredNativeChartOwnerSlides:[],fontPolicy:{basis:'design',families:[FONT]},verifyArtifactToolImport:true,receiptPath:path.join(build,`validation-${Date.now()}.json`)});
 await fs.copyFile(validated,final);console.log('PPTX structural validation and import passed.');
}else await fs.copyFile(candidate,final);
for(const [i,sl] of p.slides.items.entries())await bytes(path.join(build,`slide-${String(i+1).padStart(2,'0')}.png`),await p.export({slide:sl,format:'png',scale:1}));
await bytes(path.join(build,'montage.webp'),await p.export({format:'webp',montage:true,scale:.5}));
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
console.log('Created 12-slide PPTX + PDF and 3 editable SVG diagrams.');
