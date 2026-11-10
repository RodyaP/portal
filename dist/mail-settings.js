const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const field=(name,label,value='',type='text')=>`<label class="field"><span>${label}</span><input name="${name}" type="${type}" value="${escape(value)}" maxlength="512" autocomplete="${type==='password'?'new-password':'off'}"></label>`;
const states={pending:'Ожидает отправки',sent:'Принято SMTP',failed:'Ошибка',skipped:'Пропущено'};
const events={created:'Бронирование',updated:'Изменение',cancelled:'Отмена',finished:'Завершение',extended:'Продление',removed:'Исключение участника'};
const reason=value=>({missing_or_invalid_ad_mail:'В AD не заполнена корректная почта',replaced_by_newer_event:'Заменено новой версией встречи'}[value]||value||'—');

export async function mailAdministration(api){
 const settings=await api('/api/admin/mail');
 const panel=document.createElement('section');panel.className='access-admin';
 panel.innerHTML=`<h2>Почтовые уведомления</h2><p>Организатор и участники получают письма о бронировании, изменениях, отмене, продлении и завершении встречи. Адреса берутся из поля mail учётных записей AD. Письма отправляются каждому отдельно. При удалении участника ему приходит отдельное уведомление.</p><form id="mail-form"><label class="field"><span><input type="checkbox" name="enabled" ${settings.enabled?'checked':''}> Включить отправку уведомлений</span></label><div class="fields two">${field('host','SMTP-сервер',settings.host)}<label class="field"><span>Порт SMTP</span><input name="port" type="number" min="1" max="65535" value="${settings.port||587}" required></label><label class="field"><span>Защита соединения</span><select name="security"><option value="starttls">STARTTLS (обычно 587)</option><option value="ssl">SSL/TLS (обычно 465)</option><option value="none">Внутренний relay без TLS и пароля (обычно 25)</option></select></label>${field('sender','Адрес отправителя',settings.sender,'email')}${field('username','Логин SMTP — пусто для relay',settings.username)}${field('password',settings.password_set?'Пароль SMTP — пусто, чтобы сохранить текущий':'Пароль SMTP','','password')}</div><p>Для отправки нужен разрешённый адрес отправителя и доступ к корпоративному SMTP. Пароль хранится зашифрованным. Новые уведомления ставятся в очередь только при включённой отправке.</p><button class="btn primary" type="submit">Сохранить настройки почты</button><div class="fields two" style="margin-top:16px">${field('recipient','Адрес для тестового письма','','email')}<div><button class="btn" type="button" id="mail-test">Отправить тестовое письмо</button></div></div><p id="mail-result" role="status"></p></form><h3>Последние уведомления</h3><p>«Принято SMTP» означает приём вашим почтовым сервером. Доставку в почтовый ящик и фильтры проверяйте в почтовой системе.</p><button class="btn" id="mail-refresh">Обновить статусы</button><div id="mail-queue" aria-live="polite"></div>`;
 const footer=document.querySelector('#content .page-footer');if(footer)footer.before(panel);else document.querySelector('#content').append(panel);
 const form=panel.querySelector('#mail-form'),result=panel.querySelector('#mail-result');
 form.elements.security.value=settings.security||'starttls';
 function data(){const values=Object.fromEntries(new FormData(form));values.enabled=form.elements.enabled.checked;values.port=Number(values.port);return values}
 async function configure(test){
  if(!form.reportValidity())return;
  const values=data();if(test&&!values.recipient){result.textContent='Введите адрес для тестового письма.';return}
  const buttons=form.querySelectorAll('button');buttons.forEach(b=>b.disabled=true);result.textContent=test?'Отправляем тестовое письмо…':'Сохраняем…';
  try{await api(test?'/api/admin/mail/test':'/api/admin/mail',test?'POST':'PUT',values);result.textContent=test?'Тестовое письмо принято почтовым сервером. Проверьте почту.':values.enabled?'Отправка включена. Новые события будут отправляться по почте.':'Отправка выключена. Накопленные письма остаются в очереди.';if(!test)form.elements.password.value=''}catch(error){result.textContent=error.message}finally{buttons.forEach(b=>b.disabled=false)}
 }
 form.onsubmit=e=>{e.preventDefault();configure(false)};panel.querySelector('#mail-test').onclick=()=>configure(true);
 async function refresh(){
  const queue=panel.querySelector('#mail-queue');
  try{const data=await api('/api/admin/mail/queue');queue.innerHTML=`<p>${Object.entries(states).map(([key,label])=>`${label}: ${data.counts[key]||0}`).join(' · ')}</p>${data.items.length?`<div style="overflow-x:auto"><table class="audit-table"><thead><tr><th>СОТРУДНИК / ПОЧТА</th><th>СОБЫТИЕ</th><th>СТАТУС</th><th>ПОДРОБНОСТИ</th></tr></thead><tbody>${data.items.map(item=>`<tr><td>${escape(item.person_name)}<br><small>${escape(item.recipient||'Почта отсутствует')}</small></td><td>${events[item.event]||escape(item.event)}</td><td>${states[item.state]}<br><small>Попыток: ${item.attempts}</small></td><td>${escape(reason(item.last_error))}${item.state==='failed'?`<br><button class="btn" data-retry-mail="${item.id}">Повторить</button>`:''}</td></tr>`).join('')}</tbody></table></div>`:'<p>Уведомлений пока нет.</p>'}`;
   queue.querySelectorAll('[data-retry-mail]').forEach(button=>button.onclick=async()=>{button.disabled=true;try{await api('/api/admin/mail/queue/'+button.dataset.retryMail+'/retry','POST',{});await refresh()}catch(error){result.textContent=error.message;button.disabled=false}});
  }catch(error){queue.textContent=error.message}
 }
 panel.querySelector('#mail-refresh').onclick=refresh;await refresh();
}
