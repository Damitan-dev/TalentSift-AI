# How TalentSift access control works — and why

## Start with the three questions

A login answers **who is operating the recruiter workspace?** An invitation
answers **may this browser start this candidate's interview?** A quota answers
**are we willing to spend another interview's worth of resources now?**

Those are different decisions. Keeping the OpenAI key on the server prevents a
visitor reading the key, but an unrestricted server endpoint can still spend
it on the visitor's behalf. Protecting the recruiter page alone does not protect
the interview endpoint.

## 1. Recruiter authentication: exchanging a password for a session

When you create the account, TalentSift stores a salted **scrypt password hash**,
not the password. A hash is a one-way derived value; it is not an encrypted
password that the app later decrypts. During login, it derives a value from the
submitted password and compares the result. The salt makes identical passwords
produce different stored values; scrypt makes large guessing attacks expensive.
This implementation uses N=32768, r=8, p=3 and a random 16-byte salt.

After a successful login, the server generates a random session token. The
browser holds it in a cookie and sends the cookie with later requests. SQLite
stores only the token's hash, its recruiter role, expiry and CSRF token. The
server looks up the hash on each protected request. A username typed into a
browser field cannot create a valid session.

The session expires after eight hours, or after an hour without requests. The
refreshing dashboard counts as activity. Signing out deletes the session record;
resetting the password revokes recruiter sessions. Expiry and logout work on
the server, so keeping an old cookie does not preserve access.

In production, `Secure` restricts cookie transport to secure connections;
`HttpOnly` keeps page JavaScript from reading the cookie; `SameSite=Lax` adds
protection against cross-site requests. These flags are useful, but they do not
replace authorization or fix every JavaScript injection bug.

## 2. Authorization: checking every entrance

Authentication alone is insufficient. After learning who someone is, the server
must decide whether they may perform this action. A candidate with an invitation
still has no right to read another candidate's recording.

`AccessMiddleware` runs before route handlers. It requires a recruiter session
for every `/recruiter/...` request and the job lookup API, including direct
requests for audio or transcript downloads. The logout and score-change
endpoints are protected too. The app checks candidate access separately.

Putting a hidden button or read-only field on a page only changes the interface.
A user can edit the page or send an HTTP request directly. The server is the
place where rules must be enforced, because it controls the data and API key.

## 3. Why POST requests also have a CSRF check

A browser automatically sends cookies. A malicious website could try to make
your browser submit a request to TalentSift while you are signed in. That is
cross-site request forgery (CSRF): it abuses your existing login.

Our forms carry an unpredictable token associated with the login session.
The backend compares that token and verifies the request's Origin (or Referer
for HTTP when Origin is absent). The candidate's session-creation request sends
its token in an `X-CSRF-Token` header. Login itself has a short-lived pre-login
session and CSRF token, so it is protected before the recruiter is authenticated.

The initial invitation exchange uses its random bearer secret, a JSON request,
and a strict same-origin check. WebSockets also require the expected Origin
and the candidate cookie. Adding unrestricted cross-origin access later would
undermine these assumptions; it is not enabled here.

## 4. The invitation is a permission slip, not an identity check

You enter **David Adebayo** while creating an invitation. The server stores:

- The invitation's ID and secret-token hash.
- The job it belongs to and David's name.
- Its expiry, cancellation state, pending session and eventual start time.

The public link contains a cryptographically random secret after `#invite=`.
The browser does not send URL fragments in HTTP requests, so that secret stays
out of ordinary URL access logs. JavaScript sends it in a POST body to exchange
it for a candidate cookie, then removes it from the address bar. It is not stored
in localStorage. Request-body logging should not be enabled for token exchanges.

After checking the invitation, the backend returns the job title and assigned
name. The browser displays the name as read-only. More importantly, the session
API accepts only consent, language and recording preference. It rejects extra
name, candidate ID or job ID fields. Editing the visible name cannot alter the
identity stored with the interview.

This still does not prove that the speaker is David. Whoever possesses an unused
link can use it. Identity verification is a separate product decision; collecting
an email address alone would not solve it.

## 5. Preparation and paid admission are separate

The candidate may struggle with microphone permissions. It would be unfair to
consume their attempt simply because they viewed a page or pressed Continue.

`POST /api/session` therefore creates or reuses a **pending** session after
consent. It does not open OpenAI. The actual WebSocket route checks permission
again when the microphone is ready. Expiry, cancellation or capacity can change
between those steps, so checking only once would leave a gap.

At admission, the server atomically verifies the invitation and session, checks
the quotas, marks the invitation used, records the start and reserves a slot.
Only after that succeeds does it call the existing interview relay.

A failed paid start remains counted. We cannot reliably infer zero provider cost
from a network error. The recruiter can issue a fresh invitation when a retry is
justified. The old token never silently becomes valid again.

## 6. Why the database transaction matters

Imagine there is one remaining slot and two requests arrive together:

1. Request A reads “one slot left.”
2. Request B also reads “one slot left.”
3. Both start an interview.

The individual checks were correct when read, but the combined outcome broke
the limit. This is a race condition.

SQLite's `BEGIN IMMEDIATE` lets one admission transaction at a time perform the
read/check/write sequence. B waits for A to finish, then sees A's updated count.
Either all of a start's records commit together, or none do. Tests send several
simultaneous requests to verify that only the permitted number succeeds.

Keeping these records in the existing persistent database also means restarting
the Python process does not reset the allowance. A variable in memory would
reset on restart and would be separate in each worker process.

## 7. Duration, throughput and concurrency each bound something different

The daily limit bounds the number of starts. The concurrent limit bounds the
number of paid connections active together. The duration limit bounds how long
one connection can run. A server deadline also covers a browser that never
acknowledges the opening playback, which would otherwise prevent the ordinary
interview timer from starting.

There is also a microphone byte budget. At 24,000 PCM16 samples per second,
normal mono input is `24,000 × 2 = 48,000 bytes/second`. A token bucket replenishes
48,960 bytes of credit per second (a 2% clock margin) and permits a bounded
480,000-byte burst (ten seconds of normal PCM16 audio). Browser capture converts
to 24 kHz using the actual audio-buffer rate and keeps fractional samples across
chunks; microphone preparation permits only one active capture pipeline.
A modified client cannot send an hour of audio immediately and claim it was a
short interview. This budget runs before audio is forwarded to either paid
stream.

A rate-budget rejection is recorded as a microphone streaming-rate failure,
rather than a generic lost connection. Its terminal log contains the session ID,
accepted audio duration, elapsed stream time, available credit and rejected chunk
size. It never includes microphone contents. The margin handles small clock
differences; it does not permit sustained double-rate audio or unlimited uploads.

A concurrent-slot lease has a deadline. If the server process crashes, normal
cleanup cannot run. Once that deadline passes, the next usage/admission check
marks the stale interview failed and frees capacity. It retains the historical
start count and consumed invitation.

These controls bound use; they do not predict an exact bill. The provider's
monthly enforced spending limit is an additional account-level control.

## Where to read or change the implementation

| File | Responsibility | Why it is separate |
| --- | --- | --- |
| `access_control.py` | Password hashing, sessions, invitation records, transactions, quotas and PCM budget | Keeps the access rules independent of HTML and audio relay details. |
| `access_routes.py` | Login/logout, invitation endpoints, cookies, CSRF and route protection | Connects those rules to HTTP/WebSocket requests. |
| `manage_access.py` | Create/reset the one recruiter account and generate deployment password hashes | Account creation is an administrator action, not public signup. |
| `app.py` | Invitation-bound preparation and admission before the existing relay; deadline cleanup; authenticated score audit actor | Paid work must start only after permission and capacity are checked. |
| `client/index.html` | Exchange invitation, display the assigned name and send consent/CSRF | Makes the permitted flow clear without treating UI state as authority. |
| `templates/login.html`, `invitation_created.html`, recruiter templates | Login, invite creation/cancellation, status, usage and sign-out UI | Recruiters need a complete way to operate the new rules. |
| `client/styles.css` | Styling for those controls | Keeps the pages consistent with the existing interface. |
| `recording_storage.py`, `recording_routes.py` | Preserve recording behavior when pending setup repeats; apply the shared access boundary to recording routes | Protect audio and avoid resetting already saved recordings. |
| `tests/test_access_control.py`, `tests/test_invitation_ui.cjs` | Adversarial API, concurrency, cookie, quota and UI checks | Test the entrances an attacker or buggy client could use, not just button clicks. |

No new authentication package is required: FastAPI/Starlette provide request
and cookie handling, and Python supplies cryptographic randomness, scrypt and
SQLite. This is intentionally a single-recruiter beta. Multiple companies would
also require ownership checks linking each job/interview to its organization.
A login alone would not provide that isolation.

## Sources behind the security choices

- [OWASP password storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
- [OWASP session management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)
- [OWASP CSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
- [OpenAI spending limits](https://developers.openai.com/api/docs/guides/spend-limits)

A useful way to inspect future changes is to ask: **what happens if someone skips
the page and calls the endpoint directly?** If the rule still holds, its real
protection is probably in the right layer.
