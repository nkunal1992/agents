# Speaker notes — Raising Quality with Four Copilot Agents

One theme: **each agent makes the quality of our work better, and a human
always approves before anything changes.**

Talk like you're explaining to a colleague, not reading a spec.
~15 minutes + questions. Agents in alphabetical order, same as the slides.

---

## Opening (30 seconds)

- "Quality doesn't usually fail in one big way. It leaks in small ones —
  code nobody can test, specs nobody wrote, tickets nobody understood,
  security fixes nobody wanted to touch."
- "We built four Copilot agents. Each one plugs a different leak."
- "The rule in all four: **the agent proposes, a human approves.**"

---

## 1. code-to-requirements 📐

- **What it is, in one line:** "It reads code and writes the requirements
  document the code never had."
- **Analogy:** "Like tasting a finished dish and writing down the exact
  recipe — so exact that another kitchen could cook the same dish."
- **Why quality improves:** "Today, rebuilding or auditing a system means
  *guessing* what the old code does. This agent writes down what the code
  *actually* does — and it checks every rule against the real code before
  writing it."
- **The honesty bit:** "If it can't figure something out, it writes
  `UNRESOLVED` — it never fills gaps with a guess. An honest 'we don't
  know' is worth more than a confident wrong answer."
- **How the team uses it:** "Before a rebuild or an audit — get the real
  spec first. Then we can even generate tests straight from that spec."
- **One-line closer:** "The code already knows the rules. This just
  writes them down — and proves them."

---

## 2. cve-remediator 🛡️

- **What it is, in one line:** "When a security scan finds vulnerable
  libraries, it fixes them — carefully."
- **Analogy:** "Like a pharmacist: the *smallest dose* that cures,
  checking the medicine is actually in stock before prescribing."
- **Why quality improves:** "Fixing a vulnerability usually means
  upgrading a library — and big upgrades break things. This agent picks
  the *smallest safe upgrade*, checks it's verified, and proves it works
  in our build. Smaller change = less risk."
- **The hidden gem:** "Every project keeps a 'do not report' list for
  security warnings. Those lists go stale — they keep hiding problems
  that were fixed long ago. This agent reviews that list *every single
  run* and cleans it up."
- **How the team uses it:** "A scan fails → paste the log → it reports →
  we say 'all safe' → it applies the low-risk fixes, runs our tests, and
  writes the pull request notes for us."
- **One-line closer:** "Real exposure goes down. Nothing hides."

---

## 3. java-modernizer ☕

- **What it is, in one line:** "A senior architect looking over your
  shoulder, suggesting small improvements to the code you're already
  changing."
- **Analogy:** "Not demolishing the house — improving the room you're
  already renovating, while you're in it."
- **Why quality improves:** "Our old Java code is hard to test — and
  untested code is where bugs hide. This agent finds *why* a class can't
  be tested and fixes exactly that. Suddenly we can write tests for code
  that never had any."
- **The safety bit:** "It explains *why* for every suggestion, rates the
  risk, and only changes what we approve. It never touches more than the
  code we're already working on."
- **How the team uses it:** "Run it on every change in old code. Each
  change leaves the codebase a little cleaner and a little more testable."
- **One-line closer:** "Not a big-bang rewrite. Quality goes up with
  every single commit."

---

## 4. jira-ticket-writer 🎫

- **What it is, in one line:** "You finished the work — it writes the
  JIRA ticket."
- **Analogy:** "Like a translator: it turns 'what the code changed' into
  'what everyone needs to know' — in one click."
- **Why quality improves:** "Vague tickets cause bad reviews, wrong
  testing, and reopened issues. This agent writes tickets that describe
  what the code *actually* does — with real test steps for QA, straight
  from the changes."
- **The honesty bit:** "If it can't see the business reason in the code,
  it asks — it doesn't invent one. And it never sends secrets from the
  code into a ticket."
- **How the team uses it:** "Every finished piece of work → ticket draft
  → paste into JIRA. Same quality no matter who writes it."
- **One-line closer:** "Better tickets → sharper reviews → better
  testing. Quality upstream is quality everywhere."

---

## How it fits our day (one line per step)

- Old Java code? → **modernizer** before you commit
- Work finished? → **ticket-writer** before you open JIRA
- Security scan failed? → **cve-remediator**
- No documentation? → **code-to-requirements**

"Each agent makes one artifact better — and every artifact feeds the
next step. Better specs make better tests. Better tickets make better
reviews. Better reviews catch more bugs."

---

## Closing (20 seconds)

- "Quality doesn't come from one heroic cleanup. It comes from **small,
  checked, approved improvements** on work we're already doing."
- "And the last word again: **they propose — we approve.**"
- Then open for questions / demo.

---

### If someone asks "how do we know it's working?"

- Tickets: fewer "what did you mean?" replies and reopened issues
- Security: fewer open vulnerabilities, cleaner suppression lists
- Code: more classes that can actually be unit-tested
- Specs: documents that pass audits instead of creating questions
