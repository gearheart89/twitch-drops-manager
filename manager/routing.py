import re
from http.cookies import SimpleCookie


def rewrite_text(text: str, prefix: str) -> str:
    text = re.sub(r'''(["'`])/(api|static|session-viewer|login)(?=[/?"'`])''',
                  lambda m: m[1] + prefix + '/' + m[2], text)
    text = re.sub(r'''(["'`])/\1''', lambda m: m[1] + prefix + '/' + m[1], text)
    text = text.replace('io({', 'io({path: ' + repr(prefix + '/socket.io') + ',')
    text = text.replace("path: 'api/session/vnc'", "path: '" + prefix.lstrip('/') + "/api/session/vnc'")
    return re.sub(r'url\(/(?!/)', 'url(' + prefix + '/', text)


def scope_cookie(value: str, prefix: str) -> list[str]:
    cookies = SimpleCookie()
    cookies.load(value)
    for cookie in cookies.values():
        cookie['path'] = prefix + '/'
        cookie['domain'] = ''
    return [cookie.OutputString() for cookie in cookies.values()]
