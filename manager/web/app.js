let accounts = [];
let selected = localStorage.getItem('drops-selected') || '';
let frameAccount = '';
const byId = id => document.getElementById(id);
const helperNotice = document.createElement('div');
helperNotice.className = 'helper-notice';
helperNotice.setAttribute('role', 'status');
helperNotice.hidden = true;
byId('error').after(helperNotice);

const deleteButton = document.createElement('button');
deleteButton.id = 'delete-account';
deleteButton.className = 'quiet danger';
deleteButton.textContent = 'Delete account';
byId('actions').append(deleteButton);
const deleteDialog = document.createElement('dialog');
deleteDialog.innerHTML = '<form id="delete-form"><h2>Delete account</h2><p>This removes its saved login, game settings and local reward history from this manager. Your Twitch account and claimed rewards are kept.</p><label for="delete-name">Type the account name to confirm</label><strong id="delete-target"></strong><input id="delete-name" autocomplete="off" required><p id="delete-error" class="error" role="alert" hidden></p><div class="dialog-actions"><button type="button" id="cancel-delete" class="quiet">Cancel</button><button id="confirm-delete" class="primary danger">Delete account</button></div></form>';
document.body.append(deleteDialog);
let deletingAccount = '';
deleteButton.addEventListener('click', () => {
    const account = accounts.find(a => a.id === selected);
    if (!account) return;
    deletingAccount = account.id;
    byId('delete-target').textContent = account.name;
    deleteDialog.showModal();
});
byId('cancel-delete').addEventListener('click', () => deleteDialog.close());
deleteDialog.addEventListener('close', () => {
    byId('delete-form').reset();
    byId('delete-error').hidden = true;
});
byId('delete-form').addEventListener('submit', async event => {
    event.preventDefault();
    byId('confirm-delete').disabled = true;
    byId('cancel-delete').disabled = true;
    try {
        await api('/' + deletingAccount, 'DELETE', {confirm_name: byId('delete-name').value});
        deleteDialog.close();
        await refresh();
    } catch (error) {
        byId('delete-error').textContent = error.message;
        byId('delete-error').hidden = false;
        await refresh().catch(() => {});
    } finally {
        byId('confirm-delete').disabled = false;
        byId('cancel-delete').disabled = false;
    }
});

let theme = 'dark';
const themeButton = document.createElement('button');
themeButton.id = 'theme-toggle';
themeButton.className = 'quiet';
byId('logout').before(themeButton);
function applyTheme(value) {
    theme = value;
    document.documentElement.dataset.theme = theme;
    themeButton.textContent = theme === 'dark' ? 'Theme: Dark' : 'Theme: Light';
    themeButton.setAttribute('aria-pressed', String(theme === 'dark'));
    byId('workspace').contentWindow?.postMessage({type: 'drops-theme'}, location.origin);
}
applyTheme(theme);
byId('workspace').addEventListener('load', () => applyTheme(theme));
themeButton.addEventListener('click', async () => {
    themeButton.disabled = true;
    try {
        const response = await fetch('/_manager/api/settings', {method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-Manager-Request': '1'},
            body: JSON.stringify({theme: theme === 'dark' ? 'light' : 'dark'})});
        if (!response.ok) throw new Error(await response.text());
        applyTheme((await response.json()).theme);
    } catch (error) { fail(error.message); }
    finally { themeButton.disabled = false; }
});

const renewalPanel = document.createElement('section');
renewalPanel.className = 'renewal-panel';
renewalPanel.hidden = true;
renewalPanel.innerHTML = '<div><strong>Session renewal</strong><span id="renewal-status" role="status"></span></div><button id="renew-now" class="quiet">Renew now</button>';
helperNotice.after(renewalPanel);
let renewalRequest = false;
let renewalAccount = '';
const displayTime = seconds => seconds ? new Date(seconds * 1000).toLocaleString() : '—';
async function refreshRenewal() {
    const account = accounts.find(a => a.id === selected);
    renewalPanel.hidden = !account?.enabled;
    if (!account?.enabled) return;
    const id = account.id;
    if (renewalAccount !== id) {
        renewalAccount = id;
        byId('renewal-status').textContent = 'Checking session…';
        byId('renew-now').disabled = true;
    }
    try {
        const response = await fetch('/accounts/' + id + '/api/session');
        if (!response.ok) throw new Error('Session status unavailable');
        const data = await response.json();
        if (selected !== id) return;
        const session = data.session || {};
        const renewal = data.renewal || {};
        let status = !data.renewal_available ? 'Sign in to enable renewal.' :
            renewal.running ? 'Renewal in progress…' : renewal.queued ? 'Renewal queued…' :
            renewal.last_error || data.renewal_error ? 'Renewal failed: ' + (renewal.last_error || data.renewal_error) :
            renewal.last_success_at ? 'Last successful renewal: ' + displayTime(renewal.last_success_at) :
            'No renewal recorded since this account worker started.';
        status += ' · Proof expires: ' + displayTime(session.expires_at) + ' · Generation: ' + (session.generation ?? '—');
        byId('renewal-status').textContent = status;
        byId('renew-now').disabled = !data.logged_in || !data.renewal_available || renewal.running || renewal.queued || renewalRequest;
        byId('renew-now').dataset.generation = session.generation ?? '';
    } catch (_) {
        if (selected === id) {
            byId('renewal-status').textContent = 'Session status unavailable; retrying shortly.';
            byId('renew-now').disabled = true;
        }
    }
}
byId('renew-now').addEventListener('click', async () => {
    const id = selected;
    renewalRequest = true;
    byId('renew-now').disabled = true;
    try {
        const response = await fetch('/accounts/' + id + '/api/session/renew', {
            method: 'POST', headers: {'X-TDM-Request': '1'}});
        if (!response.ok) throw new Error(response.status === 429 ? 'Renewal is busy or was requested within the last minute.' : await response.text());
        fail('');
    } catch (error) { fail(error.message); }
    finally { renewalRequest = false; await refreshRenewal(); }
});

const changePasswordButton = document.createElement('button');
changePasswordButton.className = 'quiet';
changePasswordButton.textContent = 'Change password';
byId('logout').before(changePasswordButton);
const passwordDialog = document.createElement('dialog');
passwordDialog.innerHTML = '<form id="password-form"><h2>Change dashboard password</h2><label for="current-password">Current password</label><input id="current-password" type="password" autocomplete="current-password" maxlength="1024" required><label for="new-password">New password (16+ characters)</label><input id="new-password" type="password" autocomplete="new-password" minlength="16" maxlength="1024" required><label for="confirm-new-password">Confirm new password</label><input id="confirm-new-password" type="password" autocomplete="new-password" minlength="16" maxlength="1024" required><p id="password-error" class="error" role="alert" hidden></p><div class="dialog-actions"><button type="button" class="quiet" id="cancel-password">Cancel</button><button class="primary" id="save-password">Save password</button></div></form>';
document.body.append(passwordDialog);
changePasswordButton.addEventListener('click', () => passwordDialog.showModal());
byId('cancel-password').addEventListener('click', () => passwordDialog.close());
passwordDialog.addEventListener('close', () => { byId('password-form').reset(); byId('password-error').hidden = true; });
byId('password-form').addEventListener('submit', async event => {
    event.preventDefault();
    byId('save-password').disabled = true;
    byId('password-error').hidden = true;
    try {
        const response = await fetch('/_manager/password', {method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-Manager-Request': '1'},
            body: JSON.stringify({current_password: byId('current-password').value,
                                  password: byId('new-password').value,
                                  confirm_password: byId('confirm-new-password').value})});
        if (!response.ok) throw new Error(await response.text());
        passwordDialog.close();
    } catch (error) { byId('password-error').textContent = error.message; byId('password-error').hidden = false; }
    finally { byId('save-password').disabled = false; }
});

function fail(message) { byId('error').textContent = message; byId('error').hidden = !message; }

async function api(path = '', method = 'GET', body) {
    const response = await fetch('/_manager/api/accounts' + path, {
        method, headers: {'Content-Type': 'application/json', 'X-Manager-Request': '1'},
        ...(body ? {body: JSON.stringify(body)} : {})
    });
    if (response.status === 401) { location.replace('/_manager/login.html'); throw new Error('Dashboard locked'); }
    if (!response.ok) throw new Error(await response.text());
    return response.json();
}

function render() {
    byId('accounts').replaceChildren();
    byId('count').textContent = accounts.length;
    for (const account of accounts) {
        const button = document.createElement('button');
        button.className = 'account' + (account.id === selected ? ' selected' : '');
        button.setAttribute('aria-pressed', String(account.id === selected));
        const avatar = document.createElement('span'); avatar.className = 'avatar'; avatar.textContent = account.name[0].toUpperCase();
        const detail = document.createElement('span'); detail.className = 'account-detail';
        const name = document.createElement('strong'); name.textContent = account.name;
        const status = document.createElement('small'); status.textContent = account.status; status.className = account.status;
        detail.append(name, status); button.append(avatar, detail);
        button.addEventListener('click', () => { selected = account.id; localStorage.setItem('drops-selected', selected); render(); refreshRenewal(); });
        byId('accounts').append(button);
    }
    const account = accounts.find(a => a.id === selected);
    byId('heading').textContent = account ? account.name : 'Your rewards, organized.';
    byId('actions').hidden = !account;
    byId('welcome').hidden = !!account;
    byId('stopped').hidden = !account || account.enabled;
    byId('workspace').hidden = !account || !account.enabled;
    if (account) {
        byId('toggle').disabled = account.deleting;
        byId('toggle').textContent = account.enabled ? 'Stop account' : 'Start account';
        if (account.enabled && frameAccount !== account.id) {
            frameAccount = account.id;
            byId('workspace').src = '/accounts/' + account.id + '/';
        } else if (!account.enabled) {
            frameAccount = '';
            byId('workspace').src = 'about:blank';
        }
    } else { frameAccount = ''; byId('workspace').src = 'about:blank'; }
}

async function refresh() {
    const settings = await fetch('/_manager/api/settings');
    if (settings.ok) applyTheme((await settings.json()).theme);
    const result = await api();
    accounts = result.accounts;
    const helperAccount = accounts.find(a => a.id === result.helper_account);
    helperNotice.hidden = !helperAccount;
    helperNotice.textContent = helperAccount ? 'Desktop login active for ' + helperAccount.name + '. Finish or cancel it before enabling another account.' : '';
    if (!accounts.some(a => a.id === selected)) selected = accounts[0]?.id || '';
    fail(result.storage_error ? 'Encrypted saving failed. Check server storage before stopping the container.' : '');
    render();
    await refreshRenewal();
}

byId('add-form').addEventListener('submit', async event => {
    event.preventDefault();
    byId('add-button').disabled = true;
    try {
        const result = await api('', 'POST', {name: byId('account-name').value.trim()});
        selected = result.id; localStorage.setItem('drops-selected', selected);
        byId('account-name').value = ''; await refresh();
    } catch (error) { fail(error.message); }
    finally { byId('add-button').disabled = false; }
});
byId('toggle').addEventListener('click', async () => {
    const account = accounts.find(a => a.id === selected);
    if (!account) return;
    byId('toggle').disabled = true;
    try { await api('/' + account.id, 'PATCH', {enabled: !account.enabled}); await refresh(); }
    catch (error) { fail(error.message); }
    finally { byId('toggle').disabled = false; }
});
byId('rename').addEventListener('click', () => {
    byId('new-name').value = accounts.find(a => a.id === selected)?.name || '';
    byId('rename-dialog').showModal();
});
byId('cancel-rename').addEventListener('click', () => byId('rename-dialog').close());
byId('rename-form').addEventListener('submit', async event => {
    event.preventDefault();
    try { await api('/' + selected, 'PATCH', {name: byId('new-name').value.trim()}); byId('rename-dialog').close(); await refresh(); }
    catch (error) { fail(error.message); }
});
byId('logout').addEventListener('click', async () => {
    await fetch('/_manager/logout', {method: 'POST', headers: {'X-Manager-Request': '1'}});
    location.replace('/_manager/login.html');
});
refresh().catch(error => fail(error.message));
setInterval(() => refresh().catch(error => fail(error.message)), 10000);
setInterval(() => refreshRenewal(), 3000);
