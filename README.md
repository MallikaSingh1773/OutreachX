# LinkedIn Outreach Bot

A desktop tool that sends personalised LinkedIn connection requests to recruiters, HR and engineers at companies you are targeting. You pick the companies, pick which roles to reach out to, set how many requests to send for each, and the bot pages through search results until those numbers are filled.

It drives your own Brave browser using your existing LinkedIn session, so it never asks for your password.

![Python](https://img.shields.io/badge/python-3.8+-blue)
![Selenium](https://img.shields.io/badge/selenium-4.x-green)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)

---

## What it does

For every company you add, and for every role you tick, the bot:

1. Searches LinkedIn people search for that role at that company
2. Finds everyone with a **Connect** button (skipping Follow-only and Message-only profiles)
3. Sends each one a connection request with a note personalised to their first name and the company
4. Moves to page 2, 3, 4 and onward until your target count for that role is reached
5. Logs every action to `outreach_log.csv`

The note is templated:

```
Hi {first_name}, I'm currently exploring {role_interest} opportunities at {company}.
I came across your profile and would really appreciate connecting with you. Thank you!
```

`{first_name}`, `{company}` and `{role_interest}` are filled in automatically per person.

---

## Requirements

- Windows
- Python 3.8 or newer
- Brave browser, already logged in to LinkedIn
- A matching ChromeDriver on your PATH (Brave is Chromium based, so ChromeDriver drives it)

## Install

```bash
git clone https://github.com/<your-username>/linkedin-outreach-bot.git
cd linkedin-outreach-bot
pip install -r requirements.txt
```

## Run

**Close Brave completely first**, including any background processes in Task Manager. Selenium cannot attach to a profile that is already open.

```bash
python linkedin_bot_gui.py
```

---

## Using it

**Target Companies**
Type a company name, click **+ Add**. Repeat for as many as you want. Select a row and click **- Remove** to drop one.

**Who to message**
Tick the roles you want and set a number beside each. Only ticked roles with a count above zero will run.

| Role | Count |
|---|---|
| Recruiter | 10 |
| Talent Acquisition | 5 |
| HR / Human Resources | 5 |
| Senior Software Engineer | 10 |
| Engineering Manager | 5 |
| Tech Lead | 5 |
| Hiring Manager | 5 |

Those counts are **per company**. Three companies with 25 requests each is 75 requests total, so keep an eye on the multiplication.

**Connection Note**
Edit the text freely. Keep the three placeholders if you want personalisation. LinkedIn caps notes at 300 characters and the bot truncates anything longer.

**Role Interest**
Whatever fills `{role_interest}`. Default is "Software Engineering".

Click **Start Bot**. The live log shows progress per role and per page. **Stop Bot** finishes the request in flight and then halts.

---

## Output

`outreach_log.csv` records every attempt:

| Column | Meaning |
|---|---|
| `timestamp` | When it happened |
| `name` | Person's full name |
| `company` | Company being targeted |
| `role_searched` | Which role search found them |
| `headline` | Their LinkedIn headline |
| `profile_url` | Link to their profile |
| `note_sent` | The exact note that went out |
| `status` | `SENT`, `SKIPPED` or `FAILED` |

This file is gitignored. It holds real people's names and profile links, so it stays on your machine.

---

## How it works

LinkedIn is awkward to automate for three reasons, and most scraping tutorials break on all three.

### 1. Class names are obfuscated

LinkedIn ships hashed CSS class names such as `_19e60d0c` and `d80a7063`, and they change on every deploy. Selectors like `.entity-result__item` do not exist any more, so anything written against them silently finds zero results.

The bot ignores classes entirely and anchors on the accessibility label instead, which is stable because screen readers depend on it:

```python
a[aria-label="Invite Jane Doe to connect"]
```

That one attribute gives both the clickable element and the person's name, so no separate name lookup is needed.

### 2. The invite dialog lives in a shadow DOM

The "Add a note" modal renders inside `div#interop-outlet`'s shadow root. Neither `document.querySelector` nor Selenium's `find_element` can see into it, which is why a naive script clicks Connect and then stalls.

The bot reaches in through the shadow root:

```js
document.querySelector('#interop-outlet').shadowRoot
  .querySelector('button[aria-label="Add a note"]')
```

Inside that shadow root:

| Element | Selector |
|---|---|
| Add a note | `button[aria-label="Add a note"]` |
| Note field | `textarea#custom-message` |
| Send | `button[aria-label="Send invitation"]` |
| Dismiss | `button[aria-label="Dismiss"]` |

The note is written using the native `HTMLTextAreaElement` value setter followed by an `input` event, because React ignores a plain `.value =` assignment and the Send button would stay disabled.

### 3. Action buttons load lazily

Connect buttons render only after the row has been scrolled near. Collecting too early returns zero on a page that is full of people. The bot scrolls in eight slow steps, returns to the top, waits for things to settle, and only then reads the page.

### Pagination

Paging uses the `&page=N` URL parameter rather than clicking the numbered buttons, since the button set shifts as you go deeper.

Plenty of pages contain no Connect buttons at all, because everyone on them is Follow-only or Message-only. That is normal rather than an error, so the bot moves on. It gives up on a role after 12 pages or 4 consecutive empty pages.

---

## Rate limiting

Delays are randomised so the traffic does not look mechanical.

| Between | Delay |
|---|---|
| Requests | 5 to 12 seconds |
| Pages | 4 to 8 seconds |
| Roles | 6 to 12 seconds |
| Companies | 15 to 28 seconds |

The bot warns you before starting if a run plans more than 100 requests.

LinkedIn restricts accounts that send too many invitations, with the usual threshold sitting somewhere around 100 per week for a normal account. Weekly limits and how aggressively they are enforced are not published and do change, so treat any specific number as a rough guide rather than a safe ceiling. Invitations that sit unanswered count against you as well, so withdrawing old pending invites is worth doing periodically.

---

## Troubleshooting

**`session not created: Chrome instance exited`**
Brave is still running. Close every window and end any `brave.exe` processes in Task Manager.

**Bot finds zero connectable people on every page**
Usually genuine, meaning those results are all existing connections or Follow-only profiles. If it happens on every page for every role, LinkedIn has probably changed its markup and the selectors in the header comment of `linkedin_bot_gui.py` need rechecking.

**`no-modal` or `modal-stuck` in the log**
The invite dialog did not open or did not close. Frequently a LinkedIn weekly invite limit being hit. Check by sending one invitation by hand.

**Brave not found**
Edit `BRAVE_PATHS` at the top of `linkedin_bot_gui.py` to point at your `brave.exe`.

---

## Files

| File | Purpose |
|---|---|
| `linkedin_bot_gui.py` | The GUI application, this is the one to run |
| `linkedin_bot.py` | Earlier command line version, kept for reference |
| `config.json` | Defaults for the CLI version |
| `requirements.txt` | Python dependencies |
| `outreach_log.csv` | Generated at runtime, gitignored |

---


