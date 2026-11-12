"""Dev-only helper page to test Firebase phone login end to end (disabled when ENV=production)."""
import json

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.config import settings

router = APIRouter(include_in_schema=False)

PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Firebase login test</title>
<style>body{font-family:system-ui,sans-serif;max-width:560px;margin:32px auto;padding:0 16px;color:#111}
input,select,button{font:inherit;padding:10px;margin:4px 0;width:100%;box-sizing:border-box}button{cursor:pointer;background:#1a56db;color:#fff;border:0;border-radius:6px}
pre{background:#f3f4f6;padding:12px;white-space:pre-wrap;word-break:break-all;font-size:13px}.warn{background:#fef3c7;padding:10px;border-radius:6px}</style></head><body>
<h2>DUMBBEL · Firebase login test</h2>
__WARN__
<label>Phone (with +91)</label><input id="phone" value="+919999999999">
<label><input type="checkbox" id="testmode" checked style="width:auto"> Using a Firebase <b>test number</b> (skips reCAPTCHA, no real SMS)</label>
<label>App</label><select id="app"><option>member</option><option>admin</option><option>web</option></select>
<button id="send">1 · Send code (Firebase)</button><div id="recaptcha"></div>
<label>Code</label><input id="code" value="123456"><button id="verify">2 · Verify + log in to backend</button>
<h3>Result</h3><pre id="out">—</pre>
<script src="https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js"></script>
<script src="https://www.gstatic.com/firebasejs/10.12.2/firebase-auth-compat.js"></script>
<script>
const cfg = __CFG__; const out = m => document.getElementById('out').textContent = typeof m === 'string' ? m : JSON.stringify(m, null, 2);
firebase.initializeApp(cfg); const auth = firebase.auth(); let confirmation, verifier;
document.getElementById('send').onclick = async () => {
  const test = document.getElementById('testmode').checked;
  try {
    auth.settings.appVerificationDisabledForTesting = test;
    if (verifier) { try { verifier.clear(); } catch (_) {} document.getElementById('recaptcha').innerHTML = ''; }
    // real numbers: visible "I'm not a robot" box (most reliable); test numbers: invisible
    verifier = new firebase.auth.RecaptchaVerifier('recaptcha', {size: test ? 'invisible' : 'normal'});
    if (!test) { await verifier.render(); out('Tick the reCAPTCHA box below, then wait…'); }
    confirmation = await auth.signInWithPhoneNumber(document.getElementById('phone').value.replace(/\\s/g, ''), verifier);
    out('Code sent by Firebase ✔  Enter it and press 2.');
  } catch (e) {
    let hint = '';
    if (e.code === 'auth/captcha-check-failed' && test) hint = '\\n\\nHINT: "test number" is ticked but this phone is not in Firebase → Authentication → Sign-in method → Phone → "Phone numbers for testing" (must match exactly, e.g. +919999999999). Add it there, or untick the box to send a real SMS.';
    if (e.code === 'auth/captcha-check-failed' && !test) hint = '\\n\\nHINT: add "localhost" in Firebase → Authentication → Settings → Authorized domains, and turn off ad/tracker blockers for this page.';
    out('Firebase error: ' + e.code + '\\n' + e.message + hint);
  }
};
document.getElementById('verify').onclick = async () => {
  try {
    const res = await confirmation.confirm(document.getElementById('code').value.trim());
    const idToken = await res.user.getIdToken(); const app = document.getElementById('app').value;
    const r = await fetch('/v1/auth/firebase', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-App': app, 'X-App-Version': '1.0.0'},
      body: JSON.stringify({id_token: idToken, app, device: {name: 'Browser test', platform: 'web'}})});
    out({backend_status: r.status, response: await r.json()}); await auth.signOut();
  } catch (e) { out('Error: ' + (e.code || '') + '\\n' + e.message); }
};
</script></body></html>"""


@router.get("/dev/firebase-test", response_class=HTMLResponse)
def firebase_test():
    cfg = {"apiKey": settings.firebase_web_api_key or "", "authDomain": f"{settings.firebase_project_id}.firebaseapp.com",
           "projectId": settings.firebase_project_id}
    warn = "" if settings.firebase_web_api_key and settings.firebase_project_id else \
        '<p class="warn">Set <b>FIREBASE_WEB_API_KEY</b> (and FIREBASE_PROJECT_ID) in .env and restart the server.</p>'
    return PAGE.replace("__CFG__", json.dumps(cfg)).replace("__WARN__", warn)
