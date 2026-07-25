(() => {
  const status = document.getElementById('status');
  const webApp = window.Telegram && window.Telegram.WebApp;
  if (!webApp || !webApp.initData) {
    status.textContent = 'Откройте админ-панель через Telegram Mini App.';
    status.classList.add('error');
    return;
  }
  webApp.ready();
  webApp.expand();
  fetch('/admin/miniapp/auth', {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'},
    body: new URLSearchParams({init_data: webApp.initData}),
  }).then((response) => {
    if (!response.ok) throw new Error('access-denied');
    window.location.replace('/admin/requests');
  }).catch(() => {
    status.textContent = 'Доступ разрешён только сотрудникам, привязанным к Telegram ID.';
    status.classList.add('error');
  });
})();
