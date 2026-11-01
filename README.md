# BorrowHood — Neighborhood Tool Library

BorrowHood is a responsive, local-first web application prototype for borrowing and sharing tools within a neighborhood or apartment community. It is built with **FastAPI, SQLite, vanilla JavaScript, HTML, and CSS** so a beginner can run and inspect it in VS Code.

## Run it in VS Code (Windows)

1. Install Python 3.11 or newer.
2. Extract the ZIP and open the `borrowhood` folder in VS Code.
3. Open **Terminal → New Terminal** and run:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   uvicorn app.main:app --reload
   ```

4. Open <http://127.0.0.1:8000> in your browser.
5. Create an account and name your first community. The app displays an invite code; another account can register and use **Join a community** to join it.
6. API documentation is available at <http://127.0.0.1:8000/docs>.

If PowerShell blocks activation, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that terminal and retry activation.

## Included in this prototype

- Account registration/sign-in with password hashing and an HTTP-only, signed session cookie.
- Community membership and server-side community scoping for listings and bookings.
- Listing creation, editing, hiding, restoring, search and category filtering.
- Booking requests, owner approval, date-overlap protection and cancellation rules.
- One-time handover/return QR codes, server-side token validation, and camera scanning where the browser supports `BarcodeDetector`.
- Booking messages, return disputes that remain unresolved, and post-return reviews/Karma points.
- A signed demo payment webhook that rejects unsigned requests and handles duplicate event IDs idempotently.
- Responsive layout and labelled forms, keyboard-focus styles, and readable error notifications.
- Automated tests for listing ownership, community access, overlapping bookings, QR token reuse, return disputes, and signed/idempotent payment events.

## Important limits — read before deployment

This is a **working development MVP, not a production-ready financial marketplace**.

- Payment endpoints are demonstration-only. No real card is charged, no actual deposit is held, and the demo webhook is not a Stripe webhook integration. For real payments, integrate Stripe (or a supported provider), verify its official webhook signature with the provider SDK, and design authorization/refund rules carefully.
- QR camera scanning depends on browser support for `BarcodeDetector` and camera permission. Manual one-time code entry is available as a fallback. Use HTTPS for camera access outside localhost.
- The current dispute workflow records a dispute but does not include an admin review dashboard or resolution action yet. A disputed return intentionally does not auto-complete.
- Notifications/reminders, geolocation/map search, image uploads, moderation, rate limiting, account recovery, email verification, admin audit logs, and full backup automation are not implemented yet.
- The default session and demo webhook secrets are for local development only. Before any deployment, set long random values in environment variables, use HTTPS, enable secure cookies, configure database backups, and review privacy/security requirements.
- SQLite is suitable for local demos. A multi-user production launch should use a managed database such as PostgreSQL, migrations, and tested backup/restore procedures.
- Community isolation is enforced in the API, but community switching UI and more complete role/permission management should be added before a public launch.

## Run tests

```powershell
python -m pytest -q
```

The current automated suite passes **4 tests**. Passing tests verify the covered cases only; they do not certify the entire application as production-safe.

## Main project structure

```text
borrowhood/
├── app/
│   ├── main.py             # API, data model setup, auth and business rules
│   └── static/
│       ├── index.html      # Accessible app layout
│       ├── styles.css      # Responsive styling
│       └── app.js          # Frontend interactions and API calls
├── tests/test_app.py       # API/security workflow tests
├── .env.example            # Development configuration template
├── .gitignore
├── requirements.txt
└── README.md
```
