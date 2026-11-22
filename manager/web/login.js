fetch('/_manager/status').then(response => response.json()).then(status => {
    if (!status.configured) location.replace('/_manager/setup.html');
});
document.getElementById('login-form').addEventListener('submit', async event => {
    event.preventDefault();
    const button = document.getElementById('unlock');
    const error = document.getElementById('error');
    button.disabled = true;
    error.hidden = true;
    try {
        const response = await fetch('/_manager/login', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Manager-Request': '1'}, body: JSON.stringify({password: document.getElementById('password').value})});
        if (!response.ok) throw new Error(await response.text());
        document.getElementById('password').value = '';
        location.replace('/');
    } catch (failure) { error.textContent = failure.message; error.hidden = false; }
    finally { button.disabled = false; }
});
