"""python -m agentry                       run the server
python -m agentry reset-password USER     set a local user's password (prompts)
python -m agentry add-admin USER          create a local admin (prompts)
python -m agentry local-login on|off      turn password sign-in on or off (if SSO is broken)"""
import getpass
import sys


def _password() -> str:
    pw = getpass.getpass("New password: ")
    if pw != getpass.getpass("Again: "):
        sys.exit("Passwords don't match.")
    if len(pw) < 10:
        sys.exit("Use at least 10 characters.")
    return pw


def main(argv: list[str]) -> None:
    if not argv:
        import uvicorn
        from .config import settings
        # Proxy headers are handled by DynamicProxyHeaders, configured on the Settings page.
        uvicorn.run("agentry.app:app", host=settings.host, port=settings.port, proxy_headers=False)
        return
    from . import auth, db, prefs
    db.conn()
    cmd = argv[0]
    if cmd == "reset-password" and len(argv) == 2:
        u = db.one("SELECT * FROM users WHERE username=? AND local=1", (argv[1],))
        if not u:
            sys.exit(f"No local user {argv[1]}.")
        db.ex("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(_password()), u["id"]))
        db.audit("cli", "user.password", {"id": u["id"]})
        print("Password changed.")
    elif cmd == "add-admin" and len(argv) == 2:
        try:
            auth.create_local_user(argv[1], _password(), "admin")
        except Exception as e:
            sys.exit(getattr(e, "detail", str(e)))
        db.audit("cli", "user.add", {"username": argv[1], "role": "admin"})
        print(f"Created admin {argv[1]}.")
    elif cmd == "local-login" and len(argv) == 2 and argv[1] in ("on", "off"):
        prefs.update({"local_login": argv[1] == "on"})
        db.audit("cli", "settings.local_login", {"value": argv[1]})
        print(f"Password sign-in {argv[1]}. Restart Agentry if it's running.")
    else:
        sys.exit(__doc__)


main(sys.argv[1:])
