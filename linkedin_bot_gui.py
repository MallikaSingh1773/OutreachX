"""
LinkedIn Outreach Bot - GUI (Brave)

Uses your existing Brave profile (LinkedIn already logged in) - no credentials.

Pick which roles to target (multi-select) and how many requests to send for
each. The bot pages through search results (page=1,2,3...) until each role's
quota is filled.

SELECTOR NOTES (verified against live LinkedIn):
  LinkedIn ships obfuscated CSS class names (_19e60d0c, d80a7063 ...) that
  change every deploy, so class-based selectors are useless. Stable anchors:

    1. Connect control -> a[aria-label="Invite <Name> to connect"]
       The person's name is INSIDE the aria-label.

    2. The invite modal lives in a SHADOW DOM under div#interop-outlet:
         button[aria-label="Add a note"]
         textarea#custom-message
         button[aria-label="Send invitation"]
         button[aria-label="Dismiss"]
       Neither Selenium nor querySelector can pierce shadow DOM, so the modal
       is driven via .shadowRoot in JS.

    3. Pagination -> URL "&page=N" (verified working). Many pages contain zero
       Connect buttons (Follow-only / Message-only people) - that is normal,
       the bot just moves to the next page.

    4. Action buttons are lazy-rendered: you MUST scroll slowly and settle
       before collecting, or you get zero results on a page that has people.

Usage:
    pip install selenium
    python linkedin_bot_gui.py

IMPORTANT: Close Brave completely before starting the bot.
"""

import csv
import time
import random
import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from datetime import datetime

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.common.exceptions import (
        TimeoutException, NoSuchElementException, JavascriptException
    )
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "selenium"])
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.common.exceptions import (
        TimeoutException, NoSuchElementException, JavascriptException
    )


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "outreach_log.csv")

BRAVE_PATHS = [
    os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    os.path.expandvars(r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    os.path.expandvars(r"%PROGRAMFILES(X86)%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
]
BRAVE_PROFILE = os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data")

# Role label -> LinkedIn search keyword
ROLE_CHOICES = [
    ("Recruiter",                 "Recruiter"),
    ("Talent Acquisition",        "Talent Acquisition"),
    ("HR / Human Resources",      "Human Resources"),
    ("Senior Software Engineer",  "Senior Software Engineer"),
    ("Engineering Manager",       "Engineering Manager"),
    ("Tech Lead",                 "Tech Lead"),
    ("Hiring Manager",            "Hiring Manager"),
]

MAX_PAGES = 12   # safety cap on how deep to paginate per role


def find_brave():
    for p in BRAVE_PATHS:
        if os.path.exists(p):
            return p
    return None


JS_COLLECT = r"""
const links = [...document.querySelectorAll('a[aria-label*="to connect"]')];
return links.map(a => {
  const label = a.getAttribute('aria-label') || '';
  const m = label.match(/^Invite (.+) to connect$/);
  let box = a.parentElement;
  for (let i = 0; i < 8 && box; i++) {
    const t = (box.innerText || '').trim();
    if (t.split('\n').filter(Boolean).length >= 3) break;
    box = box.parentElement;
  }
  let headline = '', url = '';
  if (box) {
    const lines = (box.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    headline = lines[1] || '';
    const pl = box.querySelector('a[href*="/in/"]');
    if (pl) url = (pl.href || '').split('?')[0];
  }
  return { name: m ? m[1] : label, label: label, headline: headline, url: url };
});
"""

JS_TYPE_NOTE = r"""
const note = arguments[0];
const host = document.querySelector('#interop-outlet');
if (!host || !host.shadowRoot) return 'no-shadow';
const ta = host.shadowRoot.querySelector(
    'textarea#custom-message, textarea[name="message"]');
if (!ta) return 'no-textarea';
const setter = Object.getOwnPropertyDescriptor(
    window.HTMLTextAreaElement.prototype, 'value').set;
ta.focus();
setter.call(ta, note);
ta.dispatchEvent(new Event('input',  { bubbles: true }));
ta.dispatchEvent(new Event('change', { bubbles: true }));
return 'ok:' + ta.value.length;
"""

JS_SHADOW_CLICK = r"""
const host = document.querySelector('#interop-outlet');
if (!host || !host.shadowRoot) return 'no-shadow';
const el = host.shadowRoot.querySelector(arguments[0]);
if (!el) return 'not-found';
if (el.disabled) return 'disabled';
el.click();
return 'clicked';
"""

JS_SHADOW_PROBE = r"""
const host = document.querySelector('#interop-outlet');
if (!host || !host.shadowRoot) return false;
return !!host.shadowRoot.querySelector(arguments[0]);
"""


class LinkedInBot:
    def __init__(self, log_callback):
        self.driver = None
        self.running = False
        self.log_cb = log_callback
        self.sent_count = 0
        self.seen = set()
        self.csv_init()

    def csv_init(self):
        if not os.path.exists(LOG_FILE):
            with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([
                    "timestamp", "name", "company", "role_searched",
                    "headline", "profile_url", "note_sent", "status"
                ])

    def csv_log(self, name, company, role, headline, url, note, status):
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                name, company, role, headline, url, note, status
            ])

    def log(self, m):
        self.log_cb(m)

    # ── browser ──
    def start_browser(self):
        brave = find_brave()
        if not brave:
            self.log("[X] Brave not found. Install it or edit BRAVE_PATHS.")
            return False
        self.log("[*] Launching Brave with your profile...")
        o = Options()
        o.binary_location = brave
        o.add_argument(f"--user-data-dir={BRAVE_PROFILE}")
        o.add_argument("--profile-directory=Default")
        o.add_argument("--disable-notifications")
        o.add_argument("--start-maximized")
        o.add_argument("--no-first-run")
        o.add_argument("--no-default-browser-check")
        o.add_experimental_option("excludeSwitches", ["enable-automation"])
        o.add_experimental_option("useAutomationExtension", False)
        try:
            self.driver = webdriver.Chrome(options=o)
            self.driver.implicitly_wait(3)
            self.log("[+] Brave launched.")
            return True
        except Exception as e:
            self.log(f"[X] Launch failed: {str(e)[:160]}")
            self.log("[!] Close Brave COMPLETELY (check Task Manager) and retry.")
            return False

    def verify_login(self):
        self.log("[*] Checking LinkedIn session...")
        self.driver.get("https://www.linkedin.com/feed/")
        time.sleep(4)
        if "feed" in self.driver.current_url or "mynetwork" in self.driver.current_url:
            self.log("[+] Logged in. Ready.")
            return True
        self.log("[!] Not logged in - log in manually in Brave (waiting 120s)...")
        try:
            WebDriverWait(self.driver, 120).until(
                lambda d: "feed" in d.current_url or "mynetwork" in d.current_url)
            self.log("[+] Login detected.")
            return True
        except TimeoutException:
            self.log("[X] Login timed out.")
            return False

    # ── shadow modal ──
    def shadow_has(self, sel):
        try:
            return bool(self.driver.execute_script(JS_SHADOW_PROBE, sel))
        except JavascriptException:
            return False

    def shadow_click(self, sel):
        try:
            return self.driver.execute_script(JS_SHADOW_CLICK, sel)
        except JavascriptException as e:
            return f"js-error:{str(e)[:40]}"

    def wait_shadow(self, sel, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if self.shadow_has(sel):
                return True
            time.sleep(0.4)
        return False

    def close_modal(self):
        for sel in ('button[aria-label="Cancel adding a note"]',
                    'button[aria-label="Dismiss"]'):
            if self.shadow_has(sel):
                self.shadow_click(sel)
                time.sleep(1)
        if self.shadow_has('textarea#custom-message'):
            try:
                self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                time.sleep(1)
            except Exception:
                pass

    def send_invite(self, label, note):
        try:
            safe = label.replace('"', '\\"')
            link = self.driver.find_element(By.CSS_SELECTOR, f'a[aria-label="{safe}"]')
        except NoSuchElementException:
            return False, "link-gone"

        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", link)
            time.sleep(0.6)
            self.driver.execute_script("arguments[0].click();", link)
        except Exception as e:
            return False, f"click-failed:{str(e)[:30]}"

        if not self.wait_shadow('button[aria-label="Add a note"]', 10):
            self.close_modal()
            return False, "no-modal"
        if self.shadow_click('button[aria-label="Add a note"]') != "clicked":
            self.close_modal()
            return False, "add-note-failed"
        if not self.wait_shadow('textarea#custom-message', 10):
            self.close_modal()
            return False, "no-textarea"

        res = self.driver.execute_script(JS_TYPE_NOTE, note)
        if not str(res).startswith("ok:"):
            self.close_modal()
            return False, f"type-failed:{res}"
        time.sleep(1.2)

        r = self.shadow_click('button[aria-label="Send invitation"]')
        if r != "clicked":
            self.close_modal()
            return False, f"send-failed:{r}"
        time.sleep(2.5)

        if self.shadow_has('textarea#custom-message'):
            self.close_modal()
            return False, "modal-stuck"
        return True, "sent"

    # ── page loading ──
    def load_page(self, company, role_kw, page):
        q = f"{role_kw} {company}".replace(" ", "%20")
        url = ("https://www.linkedin.com/search/results/people/"
               f"?keywords={q}&origin=GLOBAL_SEARCH_HEADER")
        if page > 1:
            url += f"&page={page}"
        self.driver.get(url)
        time.sleep(4)
        # Action buttons are lazy-rendered - scroll slowly then settle,
        # otherwise a page full of people collects as zero.
        for _ in range(8):
            if not self.running:
                return []
            self.driver.execute_script("window.scrollBy(0, 420);")
            time.sleep(1.1)
        self.driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(2.5)
        try:
            return self.driver.execute_script(JS_COLLECT) or []
        except JavascriptException as e:
            self.log(f"    [!] collect failed: {str(e)[:70]}")
            return []

    # ── one role, paginating until quota filled ──
    def run_role(self, company, role_label, role_kw, quota,
                 note_template, role_interest):
        self.log(f"\n  --- {role_label}  (target: {quota}) ---")
        sent = 0
        page = 1
        empty_streak = 0

        while sent < quota and page <= MAX_PAGES and self.running:
            self.log(f"  [page {page}] loading...")
            targets = self.load_page(company, role_kw, page)

            if not targets:
                empty_streak += 1
                self.log(f"  [page {page}] no connectable people "
                         f"(follow/message only)")
                if empty_streak >= 4:
                    self.log("  4 empty pages in a row - moving on.")
                    break
                page += 1
                time.sleep(random.uniform(3, 6))
                continue

            empty_streak = 0
            self.log(f"  [page {page}] {len(targets)} connectable")

            for t in targets:
                if sent >= quota or not self.running:
                    break

                name = (t.get("name") or "").strip()
                label = t.get("label") or ""
                headline = (t.get("headline") or "").strip()
                purl = t.get("url") or ""
                if not name or not label:
                    continue

                key = purl or name
                if key in self.seen:
                    continue
                self.seen.add(key)

                first = name.split()[0]
                note = note_template.format(
                    first_name=first, company=company, role_interest=role_interest)
                if len(note) > 300:
                    note = note[:297] + "..."

                ok, reason = self.send_invite(label, note)
                if ok:
                    sent += 1
                    self.sent_count += 1
                    self.log(f"    [SENT {sent}/{quota}] {name}")
                    self.csv_log(name, company, role_label, headline, purl, note, "SENT")
                else:
                    tag = "SKIPPED" if reason == "link-gone" else "FAILED"
                    self.log(f"    [{tag}] {name} - {reason}")
                    self.csv_log(name, company, role_label, headline, purl, "", tag)

                if sent < quota:
                    d = random.uniform(5, 12)
                    time.sleep(d)

            page += 1
            if sent < quota and self.running:
                time.sleep(random.uniform(4, 8))

        if sent < quota:
            self.log(f"  --- {role_label}: {sent}/{quota} "
                     f"(ran out of people after {page-1} pages)")
        else:
            self.log(f"  --- {role_label}: {sent}/{quota} done")
        return sent

    def stop(self):
        self.running = False
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None


# ─── GUI ──────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LinkedIn Outreach Bot")
        self.geometry("780x900")
        self.configure(bg="#1a1a2e")
        self.bot = None

        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("TFrame", background="#1a1a2e")
        s.configure("TLabel", background="#1a1a2e", foreground="#e0e0e0",
                    font=("Segoe UI", 10))
        s.configure("Header.TLabel", font=("Segoe UI", 16, "bold"), foreground="#00d2ff")
        s.configure("Sub.TLabel", font=("Segoe UI", 9), foreground="#888")
        s.configure("TEntry", fieldbackground="#16213e", foreground="white")
        s.configure("TLabelframe", background="#1a1a2e")
        s.configure("TLabelframe.Label", background="#1a1a2e", foreground="#00d2ff",
                    font=("Segoe UI", 11, "bold"))
        s.configure("TCheckbutton", background="#1a1a2e", foreground="#e0e0e0",
                    font=("Segoe UI", 10))
        s.map("TCheckbutton", background=[("active", "#1a1a2e")])
        self.build()

    def build(self):
        m = ttk.Frame(self, padding=14)
        m.pack(fill="both", expand=True)

        ttk.Label(m, text="LinkedIn Outreach Bot", style="Header.TLabel").pack()
        ttk.Label(m, text="Uses your Brave profile — no login needed",
                  style="Sub.TLabel").pack(pady=(0, 8))

        # ── companies ──
        cf = ttk.LabelFrame(m, text="Target Companies", padding=8)
        cf.pack(fill="x", pady=4)
        row = ttk.Frame(cf); row.pack(fill="x")
        self.centry = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.centry, width=34)
        e.pack(side="left", padx=5); e.bind("<Return>", lambda _: self.add_company())
        tk.Button(row, text="+ Add", bg="#0077b5", fg="white", relief="flat",
                  font=("Segoe UI", 9, "bold"), width=8, cursor="hand2",
                  command=self.add_company).pack(side="left", padx=3)
        tk.Button(row, text="- Remove", bg="#555", fg="white", relief="flat",
                  font=("Segoe UI", 9, "bold"), width=8, cursor="hand2",
                  command=self.remove_company).pack(side="left", padx=3)
        self.clist = tk.Listbox(cf, height=3, bg="#16213e", fg="#e0e0e0",
                                selectbackground="#0077b5", relief="solid", bd=1,
                                font=("Segoe UI", 10))
        self.clist.pack(fill="x", pady=4, padx=5)

        # ── roles + per-role counts ──
        rf = ttk.LabelFrame(m, text="Who to message  —  tick roles, set how many each",
                            padding=8)
        rf.pack(fill="x", pady=4)

        hdr = ttk.Frame(rf); hdr.pack(fill="x", pady=(0, 3))
        ttk.Label(hdr, text="Role", font=("Segoe UI", 9, "bold"),
                  foreground="#888").pack(side="left")
        ttk.Label(hdr, text="How many", font=("Segoe UI", 9, "bold"),
                  foreground="#888").pack(side="right", padx=(0, 22))

        self.role_vars = []
        defaults = {"Recruiter": ("1", "10"),
                    "Talent Acquisition": ("1", "5"),
                    "HR / Human Resources": ("0", "5"),
                    "Senior Software Engineer": ("1", "10"),
                    "Engineering Manager": ("0", "5"),
                    "Tech Lead": ("0", "5"),
                    "Hiring Manager": ("0", "5")}

        for label, kw in ROLE_CHOICES:
            r = ttk.Frame(rf); r.pack(fill="x", pady=1)
            chk_default, cnt_default = defaults.get(label, ("0", "5"))
            var = tk.BooleanVar(value=(chk_default == "1"))
            cnt = tk.StringVar(value=cnt_default)
            ttk.Checkbutton(r, text=label, variable=var).pack(side="left")
            ttk.Entry(r, textvariable=cnt, width=5).pack(side="right", padx=20)
            self.role_vars.append((label, kw, var, cnt))

        # ── note ──
        nf = ttk.LabelFrame(m, text="Connection Note", padding=8)
        nf.pack(fill="x", pady=4)
        ttk.Label(nf, text="Placeholders:  {first_name}   {company}   {role_interest}",
                  font=("Segoe UI", 9, "italic")).pack(anchor="w")
        self.note = tk.Text(nf, height=3, bg="#16213e", fg="#e0e0e0", wrap="word",
                            relief="solid", bd=1, insertbackground="white",
                            font=("Segoe UI", 10))
        self.note.pack(fill="x", pady=4)
        self.note.insert("1.0",
            "Hi {first_name}, I'm currently exploring {role_interest} opportunities "
            "at {company}. I came across your profile and would really appreciate "
            "connecting with you. Thank you!")
        rr = ttk.Frame(nf); rr.pack(fill="x")
        ttk.Label(rr, text="Role Interest:").pack(side="left")
        self.role_interest = tk.StringVar(value="Software Engineering")
        ttk.Entry(rr, textvariable=self.role_interest, width=30).pack(side="left", padx=5)

        # ── buttons ──
        bf = ttk.Frame(m); bf.pack(fill="x", pady=8)
        self.start_btn = tk.Button(bf, text="Start Bot", bg="#0f9d58", fg="white",
                                   font=("Segoe UI", 13, "bold"), width=20,
                                   relief="flat", cursor="hand2", command=self.start)
        self.start_btn.pack(side="left", padx=10)
        self.stop_btn = tk.Button(bf, text="Stop Bot", bg="#db4437", fg="white",
                                  font=("Segoe UI", 13, "bold"), width=20,
                                  relief="flat", cursor="hand2",
                                  state="disabled", command=self.stop)
        self.stop_btn.pack(side="left", padx=10)

        ttk.Label(m, text="Close Brave completely before clicking Start",
                  foreground="#ff6b6b", font=("Segoe UI", 9, "bold")).pack()

        lf = ttk.LabelFrame(m, text="Live Log", padding=4)
        lf.pack(fill="both", expand=True, pady=4)
        self.logbox = scrolledtext.ScrolledText(lf, height=12, bg="#0f0f23",
                                                fg="#00ff41", font=("Consolas", 9),
                                                wrap="word", relief="solid", bd=1)
        self.logbox.pack(fill="both", expand=True)

        self.status = tk.StringVar(value="Ready")
        ttk.Label(m, textvariable=self.status, foreground="#888",
                  font=("Segoe UI", 9)).pack(anchor="w", pady=(4, 0))

    def add_company(self):
        v = self.centry.get().strip()
        if v:
            self.clist.insert("end", v)
            self.centry.set("")

    def remove_company(self):
        s = self.clist.curselection()
        if s:
            self.clist.delete(s[0])

    def log(self, msg):
        self.logbox.after(0, self._log, msg)

    def _log(self, msg):
        self.logbox.insert("end", msg + "\n")
        self.logbox.see("end")
        if self.bot:
            self.status.set(f"Running — {self.bot.sent_count} sent")

    def start(self):
        companies = list(self.clist.get(0, "end"))
        tmpl = self.note.get("1.0", "end").strip()
        interest = self.role_interest.get().strip()

        if not companies:
            messagebox.showwarning("Missing", "Add at least one company.")
            return
        if not tmpl:
            messagebox.showwarning("Missing", "Note template is empty.")
            return

        # collect selected roles + quotas
        plan = []
        for label, kw, var, cnt in self.role_vars:
            if not var.get():
                continue
            try:
                n = int(cnt.get())
            except ValueError:
                messagebox.showwarning("Invalid", f"'{label}' count must be a number.")
                return
            if n > 0:
                plan.append((label, kw, n))

        if not plan:
            messagebox.showwarning(
                "Missing", "Tick at least one role and give it a count above 0.")
            return

        total = len(companies) * sum(n for _, _, n in plan)
        if total > 100:
            if not messagebox.askyesno(
                "Large run",
                f"This plans up to {total} connection requests.\n\n"
                "LinkedIn often restricts accounts past ~100/week.\n\nContinue?"):
                return

        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.bot = LinkedInBot(self.log)

        def run():
            try:
                self.bot.running = True
                if not self.bot.start_browser():
                    return
                if not self.bot.verify_login():
                    return

                self.log(f"\nPlan: {', '.join(f'{n} x {l}' for l, _, n in plan)}")
                self.log(f"Companies: {', '.join(companies)}")

                for i, c in enumerate(companies):
                    if not self.bot.running:
                        break
                    self.log(f"\n{'='*54}")
                    self.log(f"  Company {i+1}/{len(companies)}: {c}")
                    self.log(f"{'='*54}")

                    for label, kw, quota in plan:
                        if not self.bot.running:
                            break
                        self.bot.run_role(c, label, kw, quota, tmpl, interest)
                        if self.bot.running:
                            time.sleep(random.uniform(6, 12))

                    if i < len(companies) - 1 and self.bot.running:
                        w = random.uniform(15, 28)
                        self.log(f"\n  cooling down {w:.0f}s before next company...")
                        time.sleep(w)

                self.log(f"\n{'='*54}")
                self.log(f"  DONE — {self.bot.sent_count} total sent")
                self.log(f"  Log: {LOG_FILE}")
                self.log(f"{'='*54}")
            except Exception as e:
                self.log(f"[X] {e}")
            finally:
                self.bot.stop()
                self.after(0, self.done)

        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        if self.bot:
            self.log("\n[!] Stopping after current request...")
            self.bot.running = False

    def done(self):
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status.set(f"Stopped — {self.bot.sent_count if self.bot else 0} sent")

    def on_close(self):
        if self.bot:
            self.bot.stop()
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
