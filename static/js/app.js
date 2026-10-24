const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));
function moneyNum(n){return Number(n||0).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2})}
function money(n){return '₹'+moneyNum(n)}
async function api(url,options={}){const r=await fetch(url,options);let data={};try{data=await r.json()}catch{}if(!r.ok){alert(data.error||'Request failed');throw new Error(data.error||'Request failed')}return data}
function toggleSidebar(){$('sidebar')?.classList.toggle('open')}
