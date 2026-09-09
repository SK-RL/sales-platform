<!-- ALWAYS read this alongside the per-ATS file. -->

## No-go (account creation required → skip, log `account_required`)
Workday (`myworkdayjobs.com`), iCIMS (`icims.com`), Taleo (`taleo.net`),
SuccessFactors (`successfactors.com`), Oracle HCM (`oraclecloud.com`),
LinkedIn Easy Apply. These need a per-company login; hand them to the
user with the URL.

## Bot detection
Greenhouse runs **invisible reCAPTCHA v3** on the application form. There
is no puzzle to solve — it scores the session and a low score can bin the
application silently, with no error shown. Practical consequences:
- Type into fields; never set values by injecting JS into the page.
- Do not machine-gun a form. Fill at human pace, one field at a time.
- Never try to influence, suppress, or solve the reCAPTCHA. If a visible
  challenge ever appears, that is a hard stop for the user.

## Always
- Hidden "honeypot" inputs (visually hidden, odd names) — never fill.
- A visible CAPTCHA / Turnstile / "verify you're human" → hard stop.
- **Dead posting:** a job URL that redirects to the board root or carries
  `?error=true` means the posting is gone. Skip, log `posting_gone`.
  (Seen live: an Alpaca job still `status=new` in our DB had been pulled.)
- After submit, confirm a success state before logging `submitted`:
  a thank-you page, "Application submitted", or the form disappearing.
