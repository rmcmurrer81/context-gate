# Company quick start

ContextGate starts as a local, fictional proof environment. A company can rename
the workspace, define what matters, connect read-only mailboxes, and export
evidence-backed reports without changing source code.

## Start the command center

On Windows, double-click **Start ContextGate** or run:

```powershell
.\run.ps1 app
```

The launcher opens `http://127.0.0.1:8501`. The main command center fits in one
browser screen: evidence and patterns on the left, decisions in the center, and
the always-available chat on the right. Choose **Layout** to put chat on the
left, middle, or right and adjust the rail and chat widths.

The old Streamlit proof lab remains available as an optional advanced view:

```powershell
.\run.ps1 lab
```

## Configure the company

Open **Settings** or **Setup** and set:

- **Company name** and **operator name**, shown together in the header.
- **Company website**, displayed with the company identity on branded exports.
- **Most important detail**, such as crowd size, invoice total, or delivery date.
- **Identity fields** that prove two updates concern the same item. For events,
  start with event name and event date.
- **Risk posture**, **mail scan limit**, and optional **auto-monitor while open** interval.
- **Voice** for spoken assistant answers when supported by the browser.
- **Company name on exports**, **custom export footer**, and an optional PNG or
  JPEG **company logo**. The logo is normalized locally and image metadata is
  removed before it is stored.

These preferences are saved locally. They do not grant access to company data.

For the presenter's local demo kit—not the neutral repository defaults—use
**Kira Labs** as the company name, `https://kiralabs.org` as the website, and the
presenter's Kira Labs logo. A safe example footer is
`Kira Labs · kiralabs.org`; do not put private contact details in a public demo
export.

## Connect Gmail or Microsoft/Hotmail

An email address is only a label; it can never log ContextGate into a mailbox.
ContextGate implements authorization-code OAuth 2.0 with PKCE and opens the
provider's real consent page. It does not ask for a mailbox password.

Each installation must first register its own provider application.

### Gmail / Google Workspace

1. Create a Google Cloud project, enable the Gmail API, configure the OAuth
   consent screen, and add test users if the app remains in testing.
2. Create an OAuth client for a **Desktop app** and download its JSON file.
3. In **Sources**, choose **Set up Gmail** and select that JSON file. ContextGate
   extracts the public client configuration; do not select or paste a service
   account key.
4. Choose **Add Google account**, approve the read-only Gmail scope on Google's
   page, then return to ContextGate.

Google's official references: [OAuth for installed apps](https://developers.google.com/identity/protocols/oauth2/native-app)
and [Gmail API Python quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python).

### Outlook.com / Hotmail / Microsoft 365

1. Register an application in Microsoft Entra ID. Allow personal Microsoft
   accounts as well as work/school accounts if Hotmail/Outlook.com is required.
2. Add the local redirect URI shown by ContextGate (normally
   `http://127.0.0.1:8501/oauth/microsoft/callback`) and enable public-client
   authorization-code flow.
3. In **Sources**, choose **Set up Microsoft**, enter the application's public
   client ID, then choose **Add Microsoft account**.
4. Approve `User.Read` and read-only `Mail.Read` on Microsoft's page.

Microsoft's official references: [MSAL Python token acquisition](https://learn.microsoft.com/en-us/entra/msal/python/getting-started/acquiring-tokens)
and [Microsoft Graph delegated authorization](https://learn.microsoft.com/en-us/graph/auth-v2-user).

ContextGate can list, scan, and remove multiple authorized accounts. Access and
refresh tokens stay in server memory for the current run and are never placed in
the browser, profile file, logs, exported reports, or Git. Restarting the app
requires reconnecting. Disconnect revokes Google access on a best-effort basis;
provider account settings remain the authoritative place to revoke consent.

Scans can be started manually or by enabling **Auto-monitor while open** in
Settings. That timer checks already configured websites and already authorized
mailboxes at the saved interval. The page clears the timer when it closes and
pauses future checks after detecting that the local server is unavailable. It
is not a durable background service.

The default scopes are read-only. ContextGate does not send email. Scanning the
first real mailbox replaces the fictional inbox in the active source catalog so
real counts cannot be inflated by demo records.

## Add a public website source

Open **Connect sources** and use **Public website intake**:

1. Enter a public `http://` or `https://` page URL. HTTPS is preferred.
2. Describe the evidence you want in the **What data should ContextGate
   collect?** field, such as `Event names, dates, times, venue addresses, and
   registration links`.
3. Choose **Add website**. Saving the definition does not fetch the page.
4. Select **Scan** beside that source for an immediate fetch. If desired, enable
   **Auto-monitor while open** in Settings and choose an interval. Remove the
   definition when no more scans should be offered.

The scanner parses structured Schema.org JSON-LD `Event` objects and iCalendar
events. When it finds no structured event, it produces a bounded page-evidence
fallback containing the title, metadata description, and a short visible-text
snippet rather than guessing missing event fields. Only parsed structured events
are added to the event catalog.

This is a bounded evidence connector, not a browser bot. It does not execute
JavaScript, log in, or bypass paywalls or access restrictions. Its optional
periodic mode is controlled by the open browser page, clears with that page, and
pauses after a local-server failure; it is not a durable scheduler. It blocks local, loopback, private-network, and unsafe redirect
destinations, including hostnames that resolve to a private address. Each scan
is limited to a 2 MB response.

When the first website scan imports a real event, ContextGate retires
the fictional event records before adding it. This prevents real counts from
being mixed with the starter catalog. Removing the saved URL stops future scans;
it does not silently remove already imported evidence. Use the explicit hide or
delete controls below for catalog visibility or deletion.

## Chat, evidence, and corrections

Use **Ask ContextGate** immediately from the right-hand panel. Useful questions
include:

- `How many events came from Eventbrite?`
- `How many events are in New York City?`
- `How did you calculate the crowd-size total?`
- `Why are there red items?`
- `What patterns do you see at 76 New Avenue?`
- `Keep track of events from Hanson Robotics.`

Count answers identify whether they came from the fictional inbox or currently
scanned email or website sources, deduplicate update messages, and show evidence
references back to the contributing message or public URL. A
human correction creates an append-only receipt; it never rewrites the original
source or decision. Corrections can be retracted.

The Hanson Robotics tracking request returns each matching fictional event's
address, date, and time and saves the instruction as explicit company guidance.
It does not turn chat into an automatic external action.

Open **Calendar** from the top bar or left company-control rail to inspect the
same visible event catalog by month. Only dates present in the loaded evidence
are placed on the grid. Select an event for its organizer, time, address, source,
and evidence reference. Records without dates stay under **Events needing a
date**; hidden and deleted source preferences are respected.

### Hide or delete a source

The two controls intentionally mean different things:

- `Do not show me data from Posh` saves a hide rule. The records remain stored,
  but the dashboard, chat, counts, and patterns leave them out.
- `Show me data from Posh again` removes the hide rule.
- `Delete data from Posh` removes matching records from this local source
  catalog and saves a deletion exclusion, so later scans do not silently import
  the same source again.

Deletion does not alter the original Gmail, Microsoft, or other upstream
account. A production organization must connect this local behavior to its
approved retention, legal-hold, and erasure workflows.

## Reports and graphics

Ask the chat for the artifact you need. Examples:

- `Create a Word and PDF report of what you are showing me.`
- `Create a pie chart of the current outcomes.`
- `Create a combo report and chart.`
- `Create a PDF report without the company name.`

ContextGate saves real `.docx`, `.pdf`, and/or `.png` files under the current
user's `Documents/ContextGate Exports` folder and replies with the exact paths.
An explicit HTML request creates `.html`. A combo request creates a report plus
a dashboard graphic. By default, documents put the configured company name at
the top and include the saved logo, website, and custom footer. “Without the
company name” is a one-export override and does not change the saved setting.

The **Export** dialog also offers browser-side artifacts:

- a printable HTML evidence report;
- a JSON audit package;
- an SVG dashboard graphic;
- a browser print/PDF view; or
- a prefilled email summary using the computer's email program.

Reports use only the current visible catalog, so hidden and deleted sources are
not included. OAuth tokens, client secrets, and mailbox passwords are never
placed in exports. The last dialog option prepares a message for a person to
review and send; ContextGate does not silently send mail, choose recipients, or
attach private evidence.

## Production hardening

The hackathon build is intentionally local and single-user. Before company-wide
deployment, add signed-in tenant identity, encrypted persistent token storage,
central secret management, provider verification, retention controls, access
logging, and an approved connector for each company data system. Never paste
passwords, OAuth tokens, client secrets, or entire private inboxes into chat,
configuration files, or Git.
