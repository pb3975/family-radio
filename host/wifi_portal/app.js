'use strict';
const $ = id => document.getElementById(id);
let busy = false;
let pollTimer;
const titles = {starting: 'Starting setup…', stopping: 'Closing setup…', cancelled: 'Setup closed', expired: 'Setup timed out', error: 'Setup unavailable', ready: 'Ready when you are', connecting: 'Connecting…', connected: 'Demo connection complete', failed: 'Let’s try again', recovery_failed: 'Recovery needs help', persistence_failed: 'Saving needs help'};
async function api(path, options) {
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || 'Could not reach setup. Try again.');
    error.status = response.status;
    throw error;
  }
  return result;
}
function setBusy(value) {
  busy = value;
  for (const field of $('join-form').elements) field.disabled = value;
  $('refresh').disabled = value;
  for (const button of $('networks').querySelectorAll('button')) button.disabled = value;
  $('connect').textContent = value ? 'Connecting…' : 'Connect radio';
}
function showStatus(state) {
  $('status').dataset.state = state.state;
  $('status-title').textContent = state.state === 'connected' && !state.demo ? 'Wi-Fi connected' : titles[state.state] || 'Setup status';
  $('status-message').textContent = state.message;
  setBusy(['starting', 'connecting', 'stopping'].includes(state.state));
  if (['recovery_failed', 'persistence_failed', 'cancelled', 'expired', 'error'].includes(state.state)) {
    setBusy(true);
    $('connect').textContent = 'Setup stopped';
  }
}
function showError(error) {
  clearTimeout(pollTimer);
  setBusy(false);
  showStatus({state: 'failed', message: error.message});
}
function securityChanged() {
  if (!busy) showStatus({state: 'ready', message: 'Choose a network to begin.'});
  $('password-group').hidden = $('security').value === 'open';
  $('open-warning').hidden = $('security').value !== 'open';
  $('password').required = $('security').value !== 'open';
  $('password').value = '';
  $('show-password').checked = false;
  $('password').type = 'password';
}
async function scan() {
  $('refresh').disabled = true;
  try {
    const data = await api('/api/networks');
    $('networks').replaceChildren();
    for (const network of data.networks) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'network';
      button.setAttribute('aria-pressed', 'false');
      const name = document.createElement('span');
      name.textContent = network.ssid;
      const security = document.createElement('small');
      security.textContent = network.security === 'open' ? 'OPEN' : 'WPA2';
      button.append(name, security);
      button.addEventListener('click', () => {
        if (busy) return;
        for (const item of $('networks').children) item.setAttribute('aria-pressed', String(item === button));
        $('ssid').value = network.ssid;
        $('security').value = network.security;
        securityChanged();
        if (network.security !== 'open') $('password').focus();
      });
      $('networks').append(button);
    }
  } catch (error) { showError(error); }
  finally { $('refresh').disabled = busy; }
}
async function poll() {
  try {
    const state = await api('/api/status');
    showStatus(state);
    if (['starting', 'connecting', 'stopping'].includes(state.state)) pollTimer = setTimeout(poll, 300);
  } catch (error) {
    if (busy && !error.status) {
      $('status-title').textContent = 'Reconnect your phone';
      $('status-message').textContent = 'Setup Wi-Fi may have closed while the radio joins. Reconnect to your chosen network. If joining failed, reconnect to setup Wi-Fi and reload this page.';
    } else showError(error);
  }
}
$('refresh').addEventListener('click', scan);
$('security').addEventListener('change', securityChanged);
$('show-password').addEventListener('change', () => { $('password').type = $('show-password').checked ? 'text' : 'password'; });
$('ssid').addEventListener('input', () => {
  if (!busy) showStatus({state: 'ready', message: 'Ready to connect to this network.'});
  for (const item of $('networks').children) item.setAttribute('aria-pressed', 'false');
});
$('join-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (busy) return;
  const body = JSON.stringify({ssid: $('ssid').value, security: $('security').value, password: $('security').value === 'open' ? '' : $('password').value});
  $('password').value = '';
  $('show-password').checked = false;
  $('password').type = 'password';
  setBusy(true);
  clearTimeout(pollTimer);
  try {
    const result = await api('/api/join', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Setup-Token': document.querySelector('meta[name=setup-token]').content}, body});
    showStatus(result);
    pollTimer = setTimeout(poll, 300);
  } catch (error) {
    if (error.status) {
      showError(error);
      return;
    }
    $('status-title').textContent = 'Reconnect your phone';
    $('status-message').textContent = 'The request may have reached the radio. Setup Wi-Fi closes while joining. Reconnect to your chosen network, or return to setup Wi-Fi and reload to check.';
  }
});
securityChanged();
scan();
poll();
