// Same-origin embedded account views follow the manager's one saved preference.
function applyManagerTheme() {
    let theme = 'dark';
    try { theme = parent.document.documentElement.dataset.theme || 'dark'; } catch (_) {}
    document.body.classList.toggle('dark-mode', theme === 'dark');
    const control = document.getElementById('dark-mode');
    if (control) { control.checked = theme === 'dark'; control.disabled = true; }
}
window.addEventListener('message', event => {
    if (event.source === parent && event.origin === location.origin && event.data?.type === 'drops-theme') applyManagerTheme();
});
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', applyManagerTheme);
else applyManagerTheme();
