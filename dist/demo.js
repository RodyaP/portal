// Browser-only synthetic dataset. Never loaded by the production API.
export const people=[
{id:'demo-anna',name:'Анна Смирнова',email:'anna@example.invalid',department:'Проектный офис',admin:false,delegate:false},
{id:'demo-max',name:'Максим Волков',email:'max@example.invalid',department:'ИТ-служба',admin:true,delegate:false},
{id:'demo-elena',name:'Елена Соколова',email:'elena@example.invalid',department:'Секретариат',admin:false,delegate:true},
{id:'demo-ivan',name:'Иван Морозов',email:'ivan@example.invalid',department:'Отдел продаж',admin:false,delegate:false},
{id:'demo-maria',name:'Мария Кузнецова',email:'maria@example.invalid',department:'Архитектурное бюро',admin:false,delegate:false},
{id:'demo-alex',name:'Алексей Орлов',email:'alex@example.invalid',department:'Строительный отдел',admin:false,delegate:false}];
const key='ugmk-meetings-demo-v1';
const today=()=>new Date().toLocaleDateString('sv-SE',{timeZone:'Asia/Yekaterinburg'});
const iso=(d,t)=>`${d}T${t}:00+05:00`;
const uid=()=>crypto.randomUUID();
const dayAdd=(d,n)=>new Date(new Date(d+'T12:00:00+05:00').getTime()+n*86400000).toLocaleDateString('sv-SE',{timeZone:'Asia/Yekaterinburg'});
const localDate=d=>new Date(d).toLocaleDateString('sv-SE',{timeZone:'Asia/Yekaterinburg'});
let state;
try{state=JSON.parse(localStorage.getItem(key))}catch{}
if(!state){
 state={bookings:[],audit:[],requests:{}};
 const seed=(day,room,start,end,title,org,parts=[],purpose='')=>state.bookings.push({id:uid(),series_id:null,room,starts:iso(day,start),ends:iso(day,end),title,purpose,organizer:people[org],creator:people[org],participants:parts.map(i=>people[i]),version:1,status:'active',kind:'meeting'});
 const d=today();
 for(let n=0;n<6;n++){
 const day=dayAdd(d,n);
 seed(day,1,'09:00','10:00','Планирование недели',0,[4,5],'Статус проектов, приоритеты и задачи команды.');
 seed(day,1,'11:00','12:30','Встреча отдела продаж',3,[],'Рабочая встреча.');
 seed(day,2,'09:30','11:00','Обсуждение проекта',4,[0,5],'Обсудить планировочные решения и согласовать следующий этап.');
 seed(day,2,'13:00','14:00','Рабочая встреча',5,[]);
 if(n===0){seed(day,1,'15:00','16:00','Встреча с командой',0,[2]);seed(day,2,'16:30','17:30','Совещание',3,[]);}
 }
 localStorage.setItem(key,JSON.stringify(state));
}
let user=people.find(p=>p.id===localStorage.getItem('ugmk-demo-user'))||people[0];
const edit=b=>user.admin||b.organizer.id===user.id||(user.delegate&&b.creator.id===user.id);
const view=b=>edit(b)||b.participants.some(p=>p.id===user.id);
const ser=b=>{let r={id:b.id,series_id:b.series_id,room:b.room,starts:b.starts,ends:b.ends,status:b.status,version:b.version,kind:b.kind,organizer:{id:b.organizer.id,name:b.organizer.name},can_edit:edit(b),can_view:view(b),title:view(b)?b.title:(b.kind==='block'?'Недоступно':'Занято')};if(view(b))Object.assign(r,{purpose:b.purpose,creator:b.creator,participants:b.participants});return r};
const save=()=>localStorage.setItem(key,JSON.stringify(state));
const fail=(message,status=422,conflicts)=>{let e=new Error(message);e.status=status;e.conflicts=conflicts;throw e};
const find=id=>{const b=state.bookings.find(b=>b.id===id);if(!b)fail('Встреча не найдена.',404);return b};
const person=id=>{const p=people.find(p=>p.id===id);if(!p)fail('Сотрудник не найден.');return p};
function dates(p){
 if(![1,2].includes(+p.room))fail('Выберите переговорную.');
 if(!p.title.trim())fail('Введите название встречи.');
 if(p.kind==='block'&&(!user.admin||!p.reason.trim()))fail('Для блокировки нужны права администратора и причина.');
 if(p.organizer&&p.organizer!==user.id&&!user.delegate)fail('Бронирование за другого доступно только secretary.',403);
 const limit=dayAdd(today(),60),until=p.repeat==='none'?p.date:p.until;
 if(!until||until>limit||until<p.date)fail('Окончание серии — в пределах 60 дней.');
 const out=[];
 for(let d=p.date;d<=until;d=dayAdd(d,1)){
 if(p.repeat==='weekly'&&!p.weekdays.includes((new Date(d+'T12:00:00').getDay()+6)%7))continue;
 if(p.exclude.includes(d))continue;
 const s=iso(d,p.start),e=iso(d,p.end),dur=(new Date(e)-new Date(s))/60000;
 if(new Date(s)<new Date()||d>limit)fail('Выберите будущее время в пределах 60 дней.');
 if(p.start<'07:00'||p.end>'21:00'||dur<15||dur>(p.kind==='block'?840:240)||+p.start.slice(3)%15||+p.end.slice(3)%15)fail('Время 07:00–21:00, шаг 15 минут, встреча до 4 часов.');
 out.push({starts:s,ends:e});
 }
 if(!out.length)fail('В серии не осталось встреч.');return out;
}
const conflicts=(rows,ignore=[])=>rows.filter(r=>state.bookings.some(b=>!ignore.includes(b.id)&&b.status==='active'&&b.room===r.room&&new Date(b.starts)<new Date(r.ends)&&new Date(b.ends)>new Date(r.starts))).map(b=>({date:localDate(b.starts),start:b.starts.slice(11,16),end:b.ends.slice(11,16)}));
function log(action,b,reason){state.audit.unshift({id:uid(),at:new Date().toISOString(),actor:user,action,booking_id:b.id,reason:reason||'',changes:{}})}
export function switchUser(id){user=person(id);localStorage.setItem('ugmk-demo-user',id)}
export async function demoApi(path,method='GET',p={}){
 const url=new URL(path,location.origin),parts=url.pathname.split('/').filter(Boolean),id=parts[2],action=parts[3];
 if(path==='/api/me')return user;
 if(url.pathname==='/api/people')return people.filter(v=>(v.name+' '+v.email).toLowerCase().includes((url.searchParams.get('q')||'').toLowerCase()));
 if(url.pathname==='/api/audit'){if(!user.admin)fail('Нет доступа.',403);return state.audit;}
 if(method==='GET'&&url.pathname==='/api/bookings'){
 return state.bookings.filter(b=>new Date(b.starts)<new Date(url.searchParams.get('end'))&&new Date(b.ends)>new Date(url.searchParams.get('start'))&&(!url.searchParams.has('mine')||b.organizer.id===user.id||b.participants.some(x=>x.id===user.id)||(user.delegate&&b.creator.id===user.id))).map(ser);
 }
 if(method==='GET'&&action==='series'){const b=find(id);if(!edit(b))fail('Нет доступа.',403);return Object.fromEntries(state.bookings.filter(x=>x.series_id===b.series_id&&new Date(x.starts)>=Math.max(new Date(b.starts),new Date())&&x.status==='active').map(x=>[x.id,x.version]));}
 if(method==='GET'){const b=find(id);if(!view(b))fail('Доступны только время, комната и организатор.',403);return ser(b);}
 const fp=JSON.stringify([path,method,p]);
 if(state.requests[p.request_id]){if(state.requests[p.request_id].fingerprint!==fp)fail('Запрос изменён. Повторите сохранение.',409);return state.requests[p.request_id].response;}
 let result;
 if(url.pathname==='/api/bookings'||id==='preview'){
 const rows=dates(p).map(d=>({...d,room:+p.room})),bad=conflicts(rows);
 if(id==='preview')return {dates:rows.map(x=>localDate(x.starts)),conflicts:bad};
 if(bad.length)fail('Время уже занято. Выберите другое или исключите даты.',409,bad);
 const series=p.repeat==='none'?null:uid();
 const created=rows.map(r=>({...r,id:uid(),series_id:series,title:p.title.trim(),purpose:p.purpose,organizer:person(p.organizer||user.id),creator:user,participants:[...new Set(p.participants)].map(person),version:1,status:'active',kind:p.kind}));
 created.forEach(b=>{state.bookings.push(b);log(b.kind==='block'?'Создание блокировки':'Создание',b,p.reason)});result={ids:created.map(x=>x.id)};
 }else{
 const b=find(id);
 let old=p.scope==='future'?state.bookings.filter(x=>x.series_id===b.series_id&&x.status==='active'&&new Date(x.starts)>=Math.max(new Date(b.starts),new Date())):[b];
 old.forEach(x=>{if(!edit(x))fail('Нет права изменять встречу.',403);if(x.status!=='active'||new Date(x.ends)<=new Date())fail('Встреча доступна только для просмотра.');if((p.scope==='future'?p.versions[x.id]:p.version)!==x.version)fail('Встреча уже изменена. Обновите карточку.',409);if(user.admin&&x.organizer.id!==user.id&&!p.reason?.trim())fail('Укажите причину изменения.');});
 let rows;
 if(method==='PUT'){
 if(old.some(x=>new Date(x.starts)<new Date()))fail('Текущую встречу можно завершить или продлить.');
 if(p.organizer!==b.organizer.id&&!user.delegate)fail('Изменение организатора доступно только secretary.');
 dates({...p,organizer:user.id,repeat:'none'});
 rows=old.map(x=>({...x,room:+p.room,starts:iso(p.scope==='future'?localDate(x.starts):p.date,p.start),ends:iso(p.scope==='future'?localDate(x.starts):p.date,p.end),title:p.title.trim(),purpose:p.purpose,organizer:person(p.organizer),participants:p.participants.map(person),version:x.version+1}));
 const bad=conflicts(rows,old.map(x=>x.id));if(bad.length)fail('Время занято. Исходная бронь сохранена.',409,bad);
 }else{
 rows=old.map(x=>{let r={...x,version:x.version+1};if(action==='cancel'){if(new Date(x.starts)<=new Date())fail('Текущую встречу можно завершить досрочно.');r.status='cancelled'}else{if(!(new Date(x.starts)<new Date()&&new Date(x.ends)>new Date()))fail('Действие доступно для текущей встречи.');if(action==='finish')r.ends=new Date().toISOString();else{r.ends=p.end;const m=(new Date(r.ends)-new Date(r.starts))/60000;const t=new Date(r.ends).toLocaleTimeString('ru-RU',{timeZone:'Asia/Yekaterinburg',hour:'2-digit',minute:'2-digit'});if(new Date(r.ends)<=new Date(x.ends)||m>240||t>'21:00'||+t.slice(3)%15)fail('Недопустимое время окончания.');if(conflicts([r],[r.id]).length)fail('Следующий интервал занят.',409);}}return r});
 }
 rows.forEach(r=>{state.bookings[state.bookings.findIndex(x=>x.id===r.id)]=r;log(method==='PUT'?'Изменение':({cancel:'Отмена',finish:'Досрочное завершение',extend:'Продление'}[action]),r,p.reason)});result={ids:rows.map(x=>x.id)};
 }
 state.requests[p.request_id]={fingerprint:fp,response:result};save();return result;
}
