fetch('/_manager/status').then(response => response.json()).then(status => {
    if (status.configured) location.replace('/_manager/login.html');
});
document.getElementById('setup-form').addEventListener('submit', async event => {
    event.preventDefault();
    const button = document.getElementById('create-password');
    const error = document.getElementById('error');
    button.disabled = true;
    error.hidden = true;
    try {
        const response = await fetch('/_manager/setup', {method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-Manager-Request': '1'},
            body: JSON.stringify({password: document.getElementById('password').value,
                                  confirm_password: document.getElementById('confirmation').value})});
        if (!response.ok) throw new Error(await response.text());
        document.getElementById('setup-form').reset();
        location.replace('/');
    } catch (failure) { error.textContent = failure.message; error.hidden = false; }
    finally { button.disabled = false; }
});
