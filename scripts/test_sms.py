"""Send a test OTP through 2Factor using the .env settings (same call the API makes).
Usage:  .venv/bin/python -m scripts.test_sms 9849011234"""
import sys

import httpx

from app.config import settings
from app.services.sms import _digits
from app.utils import normalize_phone

if __name__ == "__main__":
    if not settings.twofactor_api_key:
        sys.exit("TWOFACTOR_API_KEY is empty in .env — paste your 2Factor API key there first.")
    phone = _digits(normalize_phone(sys.argv[1]))
    tpl = settings.twofactor_otp_template
    url = f"https://2factor.in/API/V1/{settings.twofactor_api_key}/SMS/{phone}/123456/{tpl}"
    print(f"Sending OTP 123456 to {phone} with template '{tpl}' ...")
    r = httpx.get(url, timeout=15)
    print(r.status_code, r.text)
    print("2Factor accepted it. If it still arrives as a CALL, the template name/DLT mapping isn't active — see README."
          if '"Success"' in r.text else "FAILED — see 2Factor's message above")
