/* Global integration settings. No broker connection is needed until enabled. */
(() => {
    const labels = {
        account_link_required: 'Game account linking required',
        account_link_restored: 'Game account link restored',
        drop_claimed: 'Reward claimed (MQTT)',
        login_required: 'Twitch login required',
        renewal_failed: 'Session renewal failed'
    };
    const node = id => document.getElementById(id);
    const button = document.createElement('button');
    button.id = 'notifications-button'; button.className = 'quiet'; button.textContent = 'Notifications';
    node('logout').before(button);
    const notice = document.createElement('section');
    notice.id = 'link-alerts'; notice.className = 'helper-notice'; notice.hidden = true;
    notice.setAttribute('aria-label', 'Account linking alerts');
    node('error').after(notice);
    const dialog = document.createElement('dialog');
    dialog.id = 'notifications-dialog'; dialog.className = 'notifications-dialog';
    dialog.innerHTML = `<form id="notifications-form" class="notification-form">
      <h2>Notifications &amp; smart home</h2>
      <p>Use existing Telegram/Discord destinations from each account. Add an MQTT broker whenever you are ready. MQTT starts disabled.</p>
      <p id="mqtt-status" role="status"></p>
      <fieldset><legend>MQTT connection</legend>
        <label class="check"><input id="mqtt-enabled" type="checkbox"> Enable MQTT publishing</label>
        <div class="notification-grid"><label>Broker host<input id="mqtt-host" maxlength="253" placeholder="e.g. mqtt.home.arpa"></label><label>Port<input id="mqtt-port" type="number" min="1" max="65535" value="1883" required></label></div>
        <label class="check"><input id="mqtt-tls" type="checkbox"> Use TLS with certificate verification</label>
        <div class="notification-grid"><label>Username<input id="mqtt-username" maxlength="1024" autocomplete="off"></label><label>Password<input id="mqtt-password" type="password" maxlength="1024" autocomplete="new-password" placeholder="Leave blank to keep saved password"></label></div>
        <label class="check"><input id="mqtt-clear-password" type="checkbox"> Clear saved broker password</label>
        <label>Topic prefix<input id="mqtt-prefix" maxlength="128" value="twitch-drops" required></label>
        <p id="mqtt-topics"></p><button id="mqtt-test" type="button" class="quiet">Send MQTT test</button>
      </fieldset>
      <fieldset><legend>Events</legend><div id="notification-events" class="event-options"></div><p>Existing claim messages in Telegram/Discord keep their account settings; this adds linking, login and renewal alerts.</p></fieldset>
      <details><summary>Per-account delivery and event selections</summary><div id="notification-accounts"></div></details>
      <p>Link status comes from Twitch and updates when campaigns refresh. It cannot verify delivery inside the game. MQTT publishes status and events only; remote commands and HomeKit pairing are not enabled.</p>
      <p id="notification-error" class="error" role="alert" hidden></p>
      <div class="dialog-actions"><button id="notifications-cancel" type="button" class="quiet">Cancel</button><button id="notifications-save" class="primary">Save settings</button></div>
    </form>`;
    document.body.append(dialog);
    let latest;
    function checkbox(label, checked, container, group, key) {
        const row = document.createElement('label'); row.className = 'check';
        const input = document.createElement('input'); input.type = 'checkbox'; input.checked = checked;
        input.dataset.group = group; input.dataset.key = key;
        row.append(input, document.createTextNode(' ' + label)); container.append(row);
    }
    function connection(data) {
        node('mqtt-status').textContent = 'MQTT: ' + data.mqtt.connection +
            (data.mqtt.last_published_at ? ' · Last publish: ' + new Date(data.mqtt.last_published_at * 1000).toLocaleString() : '') +
            (data.error ? ' · ' + data.error : '') +
            (data.mqtt.dropped_events ? ' · Events dropped: ' + data.mqtt.dropped_events : '');
        node('mqtt-topics').textContent = 'Topic root: ' + data.mqtt.topic_root;
    }
    function fill(data) {
        const config = data.settings; const mqtt = config.mqtt;
        node('mqtt-enabled').checked = mqtt.enabled; node('mqtt-host').value = mqtt.host;
        node('mqtt-port').value = mqtt.port; node('mqtt-tls').checked = mqtt.tls;
        node('mqtt-username').value = mqtt.username; node('mqtt-password').value = '';
        node('mqtt-password').placeholder = mqtt.password_set ? 'Saved password present; leave blank to keep' : 'Broker password';
        node('mqtt-clear-password').checked = false; node('mqtt-prefix').value = mqtt.topic_prefix;
        node('notification-events').replaceChildren(); node('notification-accounts').replaceChildren();
        for (const [event, label] of Object.entries(labels)) checkbox(label, config.events[event], node('notification-events'), 'global', event);
        for (const account of accounts) {
            const section = document.createElement('section'); section.className = 'notification-account'; section.dataset.account = account.id;
            const heading = document.createElement('h3'); heading.textContent = account.name; section.append(heading);
            const rule = config.accounts[account.id] || {telegram: true, discord: true, mqtt: true, events: Object.keys(labels)};
            const routes = document.createElement('div'); routes.className = 'event-options';
            for (const route of ['telegram', 'discord', 'mqtt']) checkbox(route === 'mqtt' ? 'MQTT' : route[0].toUpperCase() + route.slice(1), rule[route], routes, 'route', route);
            section.append(routes);
            const events = document.createElement('div'); events.className = 'event-options';
            for (const [event, label] of Object.entries(labels)) checkbox(label, rule.events.includes(event), events, 'event', event);
            section.append(events); node('notification-accounts').append(section);
        }
        connection(data);
    }
    window.refreshNotifications = async () => {
        const response = await fetch('/_manager/api/notifications');
        if (!response.ok) return;
        latest = await response.json();
        if (dialog.open) connection(latest);
        notice.replaceChildren();
        let count = 0;
        for (const state of Object.values(latest.accounts)) {
            for (const alert of state.alerts || []) {
                const row = document.createElement('p');
                row.textContent = state.account + ' · ' + alert.game + ': Twitch reports linking required' +
                    (state.status === 'unavailable' || !state.link_status_fresh ? ' (last known status)' : '') + '. ';
                if (alert.link_url) {
                    try {
                        const url = new URL(alert.link_url);
                        if (url.protocol === 'https:' && !url.username && !url.password && !url.search && !url.hash) {
                            const link = document.createElement('a'); link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
                            link.textContent = 'Open account linking'; row.append(link);
                        }
                    } catch { /* A bad publisher link must not block settings. */ }
                }
                notice.append(row); count++;
            }
        }
        notice.hidden = !count;
    };
    button.addEventListener('click', async () => {
        await window.refreshNotifications();
        if (!latest) return;
        fill(latest); node('notification-error').hidden = true; dialog.showModal();
    });
    node('notifications-cancel').addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => { node('mqtt-password').value = ''; });
    node('notifications-form').addEventListener('submit', async event => {
        event.preventDefault(); node('notifications-save').disabled = true;
        node('notification-error').hidden = true;
        const config = {mqtt: {enabled: node('mqtt-enabled').checked, host: node('mqtt-host').value.trim(),
            port: Number(node('mqtt-port').value), tls: node('mqtt-tls').checked,
            username: node('mqtt-username').value, password: node('mqtt-password').value,
            clear_password: node('mqtt-clear-password').checked, topic_prefix: node('mqtt-prefix').value}, events: {}, accounts: {}};
        for (const input of dialog.querySelectorAll('[data-group="global"]')) config.events[input.dataset.key] = input.checked;
        for (const section of dialog.querySelectorAll('[data-account]')) {
            const rule = {events: []};
            for (const input of section.querySelectorAll('[data-group="route"]')) rule[input.dataset.key] = input.checked;
            for (const input of section.querySelectorAll('[data-group="event"]:checked')) rule.events.push(input.dataset.key);
            config.accounts[section.dataset.account] = rule;
        }
        try {
            const response = await fetch('/_manager/api/notifications', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Manager-Request': '1'}, body: JSON.stringify(config)});
            if (!response.ok) throw new Error(await response.text());
            latest = await response.json(); node('mqtt-password').value = ''; connection(latest); dialog.close();
        } catch (error) { node('notification-error').textContent = error.message; node('notification-error').hidden = false; }
        finally { node('notifications-save').disabled = false; }
    });
    node('mqtt-test').addEventListener('click', async () => {
        try {
            const response = await fetch('/_manager/api/notifications/test', {method: 'POST', headers: {'X-Manager-Request': '1'}});
            if (!response.ok) throw new Error(await response.text());
            node('notification-error').textContent = 'Test queued. The connection status and last-publish time confirm broker delivery.';
        } catch (error) { node('notification-error').textContent = error.message; }
        node('notification-error').hidden = false;
    });
    window.refreshNotifications().catch(() => {});
})();
