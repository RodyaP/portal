// Authentication and portal administration. No secrets are put in localStorage.
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const field = (name, label, value='', type='text', required=true) => `<label class="field"><span>${label}</span><input name="${name}" type="${type}" value="${escape(value)}" ${required?'required':''} maxlength="${type==='password'?256:1000}" autocomplete="${type==='password'?'new-password':'off'}"></label>`;

export async function loginScreen(api, boot) {
 const status = await api('/api/auth/status');
 const setup = status.setup_required;
 document.querySelector('#app').innerHTML=`<main class="access-card"><img src="/assets/logo.png" width="220" alt="УГМК-Застройщик"><h1>${setup?'Первый запуск':'Вход в портал'}</h1><p>${setup?'Создайте локального администратора. Ключ настройки указан в конфигурации сервера.':'Используйте свою корпоративную учётную запись.'}</p><form id="access-form">${setup?field('token','Ключ первоначальной настройки','','password'):''}${field('login',setup?'Логин локального администратора':'Логин AD или локального администратора')}${field('password',setup?'Пароль — не менее 12 символов':'Пароль','','password')}${setup?field('confirmation','Повторите пароль','','password'):'<label class="field"><span><input type="checkbox" name="local"> Войти как локальный администратор</span></label>'}<div id="access-error" role="alert"></div><button class="btn primary" type="submit">${setup?'Создать администратора':'Войти'}</button></form></main>`;
 document.querySelector('#access-form').elements.login.autocomplete='username';
 document.querySelector('#access-form').elements.password.autocomplete=setup?'new-password':'current-password';
 document.querySelector('#access-form').onsubmit=async event=>{
  event.preventDefault();const form=event.currentTarget,button=form.querySelector('button');
  const data=Object.fromEntries(new FormData(form));data.local=setup||data.local==='on';
  if(setup&&data.password!==data.confirmation){document.querySelector('#access-error').textContent='Пароли не совпадают.';return}
  button.disabled=true;
  try{await api(setup?'/api/auth/setup':'/api/auth/login','POST',data);await boot()}
  catch(error){document.querySelector('#access-error').textContent=error.message;button.disabled=false}
 };
}

export function logoutButton(api) {
 const parent=document.querySelector('.user-text');
 if(!parent)return;
 const button=document.createElement('button');button.className='btn';button.textContent='Выйти';
 button.onclick=async()=>{try{await api('/api/auth/logout','POST',{});location.reload()}catch(error){alert(error.message)}};
 parent.append(button);
}

export async function administration(api, me) {
 const [settings,roles]=await Promise.all([api('/api/admin/ad'),api('/api/admin/roles')]);
 const panel=document.createElement('section');panel.className='access-admin';
 panel.innerHTML=`<h2>Подключение к Active Directory</h2><p>Защищённое соединение LDAPS, порт 636. Сертификат корпоративного центра устанавливается на сервере.</p><form id="ad-form"><div class="fields two">${field('host','Контроллер AD (FQDN)',settings.host)}${field('realm','Домен, например EKATCITY.EKAT-CITY.COM',settings.realm)}${field('base_dn','Область поиска (Base DN)',settings.base_dn)}${field('bind_dn','Служебная учётка чтения (DN или UPN)',settings.bind_dn)}</div>${field('password',settings.password_set?'Пароль служебной учётки — пусто, чтобы сохранить текущий':'Пароль служебной учётки','','password',!settings.password_set)}<button class="btn" type="button" id="ad-test">Проверить подключение</button> <button class="btn primary" type="submit">Проверить и сохранить</button><p id="ad-result" role="status"></p></form><h2>Права сотрудников</h2><p>Роли действуют только в портале. Локальный администратор сохраняет аварийный доступ.</p><label class="field"><span>Поиск сотрудника AD</span><input id="role-search" placeholder="Имя или почта — от двух символов"></label><div id="role-search-results"></div><div id="role-list"></div>${me.local?`<h2>Пароль локального администратора</h2><form id="local-password">${field('current_password','Текущий пароль','','password')}${field('new_password','Новый пароль — не менее 12 символов','','password')}<button class="btn" type="submit">Изменить пароль и выйти</button><p id="password-result" role="status"></p></form>`:''}`;
 const footer=document.querySelector('#content .page-footer');
 if(footer)footer.before(panel);else document.querySelector('#content').append(panel);
 const result=document.querySelector('#ad-result'),adForm=document.querySelector('#ad-form');
 async function configure(save){
  if(!adForm.reportValidity())return;
  const buttons=adForm.querySelectorAll('button');buttons.forEach(b=>b.disabled=true);result.textContent='Проверяем подключение…';
  try{await api(save?'/api/admin/ad':'/api/admin/ad/test',save?'PUT':'POST',Object.fromEntries(new FormData(adForm)));result.textContent=save?'Настройки проверены и сохранены.':'Соединение установлено, область поиска доступна.';if(save){adForm.elements.password.value='';adForm.elements.password.required=false}}
  catch(error){result.textContent=error.message}finally{buttons.forEach(b=>b.disabled=false)}
 }
 adForm.onsubmit=e=>{e.preventDefault();configure(true)};document.querySelector('#ad-test').onclick=()=>configure(false);
 function roleRow(person,role={}) {
  const div=document.createElement('div');div.className='role-row';
  div.innerHTML=`<b>${escape(person.name)}</b> <small>${escape(person.email)}</small><label><input type="checkbox" name="admin" ${role.admin?'checked':''}> Администратор</label><label><input type="checkbox" name="delegate" ${role.delegate?'checked':''}> Бронирование по поручению</label><button class="btn">Сохранить права</button><span role="status"></span>`;
  div.querySelector('button').onclick=async event=>{event.target.disabled=true;try{await api('/api/admin/roles/'+encodeURIComponent(person.id),'PUT',{admin:div.querySelector('[name=admin]').checked,delegate:div.querySelector('[name=delegate]').checked});div.querySelector('[role=status]').textContent='Сохранено';if(person.id===me.id)location.reload()}catch(error){div.querySelector('[role=status]').textContent=error.message}finally{event.target.disabled=false}};
  return div;
 }
 roles.forEach(role=>document.querySelector('#role-list').append(roleRow(role.person,role)));
 let timer,sequence=0;
 document.querySelector('#role-search').oninput=event=>{
  clearTimeout(timer);const term=event.target.value,version=++sequence;
  const results=document.querySelector('#role-search-results');results.replaceChildren();
  if(term.trim().length<2)return;
  timer=setTimeout(async()=>{try{const people=await api('/api/people?q='+encodeURIComponent(term));if(version!==sequence)return;results.replaceChildren();if(!people.length)results.textContent='Сотрудники не найдены.';people.forEach(person=>results.append(roleRow(person,roles.find(r=>r.person_id===person.id))))}catch(error){if(version===sequence)results.textContent=error.message}},350);
 };
 if(me.local)document.querySelector('#local-password').onsubmit=async event=>{
  event.preventDefault();const form=event.currentTarget,button=form.querySelector('button');button.disabled=true;
  try{await api('/api/admin/local-password','POST',Object.fromEntries(new FormData(form)));location.reload()}catch(error){document.querySelector('#password-result').textContent=error.message;button.disabled=false}
 };
}
