# BorrowHood QA report

Automated command: `python -m pytest -q`

Latest result: **4 passed**.

| Requirement | Current status | Evidence / limitation |
|---|---|---|
| Community isolation | Partially covered | API queries scope listings to the selected community; tests cover joining a community and accessing its listings. More multi-community and adversarial tests are still needed. |
| Item ownership | Covered for listing edits | API rejects a non-owner attempting to edit another user's listing. |
| Authentication | Covered by API dependency design | Protected routes require a valid signed session cookie; add broader unauthenticated-route tests before launch. |
| Listing CRUD | Partially covered | Create, edit, hide, restore exist; automated tests focus on owner edit rejection. |
| Double booking | Covered for overlapping confirmed booking | Test confirms an overlapping request is rejected after approval. Add concurrency tests before production. |
| Cancellation | Partially covered | Pending/approved bookings can be cancelled; post-handover cancellation is blocked for admin resolution. Payment refunds are not integrated. |
| QR handover | Covered for token validation/reuse | Server generates one-time QR data and verifies parsed payloads; camera availability depends on browser support. |
| Invalid QR | Covered for expired/reused/invalid token behavior | Automated test confirms one-time QR reuse is rejected; add more malformed payload tests. |
| Return dispute | Covered for recording unresolved state | Dispute remains `return_review`; review resolution/admin UI is not implemented. |
| Payment verification | Demo only | Unsigned events are rejected; signed demo events are processed. Not a real provider integration. |
| Duplicate payment events | Covered in demo webhook | Unique event ID and automated idempotency test. No actual charges/refunds are performed. |
| Backup recovery | Not implemented/tested | Use SQLite backup/export and restore validation before production. |
| Mobile layout | Implemented visually | Responsive CSS exists; no automated device/browser matrix yet. |
| Accessible forms | Partially implemented | Labels, required fields, keyboard focus indicators and status messages exist; a full WCAG audit is still needed. |
| Notifications | Not implemented | No background reminders or push/email notifications yet. |
