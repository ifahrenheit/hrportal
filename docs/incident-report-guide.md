# Incident Report (IR) Guide for Overheads

This guide is for overheads who file or review Incident Reports in the HR Portal: QA, TL, SOM, RTA, Trainers and IT.

Portal link: **https://hrportal.cohere.ph/incident-reports/**

---

## 1. Filing an IR

1. Open **Incident Reports**, then click **New Report**.
2. Fill in the form:
   - **Date of Incident** (required). Future dates aren't allowed.
   - **Agent Involved** (required). Pick the agent from the list.
   - **Incident Summary** (required). State what happened, when, and what policy it concerns. Keep it factual.
   - **Attachments** (optional). Up to 4 images, such as screenshots or photos. Only image files are accepted.
3. Decide whether to tick **"Escalate to HR immediately — Written Explanation Required"**:
   - **Leave it unticked** if the agent's TL should look at the IR first and decide what happens next. **This is the normal path.**
   - **Tick it** if you already know the incident needs a Written Explanation (RWE) from the agent. HR is alerted right away.
4. Click **Submit**. The IR gets a number like `IR-20261001-AB12CD`.

### Who gets an email when you submit

- Managers (managers@cohere.ph)
- The agent's TL
- Everyone in **your** group. For example, all of QA gets a copy when someone from QA files.
- HR, but only if you ticked **Escalate to HR**

---

## 2. How an IR moves

```
             ┌──────────── Resolved / Waived by TL
             │
 Filed ──► PENDING ──► RWE REQUEST ──► RWE FOR SIGNATURE ──► RWE FOR SERVICE
 (TL decides)         (HR prepares)    (TL signs)            (HR serves to agent)
                                                                    │
                                                                    ▼
 RESOLVED ◄── FOR MEMO ◄── FORWARDED ◄────────────── AWAITING EXPLANATION
 (HR memo)    (HR writes)  (SOM/TL decides)           (agent writes explanation)
                              │
                              └──► WAIVED
```

| Status | Whose turn | What they do |
|---|---|---|
| **Pending** | **TL / SOM** | Review the IR. Then **Request RWE** to send it to HR, or **Resolve**, or **Waive**. |
| **RWE Request** | **HR** | Prepare the Written Explanation document. Then mark **RWE for Signature**. If HR needs to investigate first, they mark **IR Reviewed**. |
| **RWE for Signature** | **TL** | Sign the RWE document. Then mark **RWE for Service (I have signed)**. |
| **RWE for Service** | **HR** | Serve the signed RWE to the agent. Then mark **RWE Served (Awaiting Explanation)**. |
| **Awaiting Explanation** | **Agent → HR** | The agent submits their written explanation. HR then marks **Explanation Received**. If the agent hasn't replied after 5 days, HR forwards the IR with a note. |
| **Forwarded** | **SOM / TL** | Read the explanation and decide: **Proceed with Disciplinary Action** or **Waive IR**. |
| **For Memo** | **HR** | Write the memo with its category and disciplinary action. Then mark **Resolved**. |
| **IR Reviewed** | **HR** | HR is investigating, for example a possible termination. They **Resolve** the IR with a note when done. |
| **Resolved** / **Waived** | — | Closed. |

**Deadlines:** each stage should move within **5 days**. On the dashboard, an IR shows a **warning** at 4 days and turns **overdue** after 5 days.

---

## 3. Reviewing and commenting

- Open an IR from the link in the email, or from your **Incident Reports** dashboard.
  - TLs see the IRs for the agents assigned to them.
  - Other overheads see the IRs filed by their own group.
- Anyone working on the IR can **add a comment** at any stage, with up to 4 image attachments.
- Under the comment box, **Update Status** lists only the steps that are your turn. If it only says *"Post comment only"*, the IR is waiting on someone else. You can still comment.
- Only these people can change an IR's status:
  - the agent's **TL / SOM**: TL groups, the agent's assigned supervisor, or the agent's SOM
  - **HR**
  - portal admins

  QA, RTA, Trainers, IT and other overheads can view and comment, but they don't see the **Update Status** box.
- To move the IR forward, write a comment, pick the next status, and submit. Both are saved together.

### Who gets an email when someone comments or changes the status

- Managers, the agent's TL, and the group of the person who **filed** the IR
- **HR is emailed only when the IR is sent to them** (Request RWE). Ordinary comments are **not** emailed to HR.
- When HR marks **RWE Served**, the agent's TL also gets a separate "RWE Served" email.

> **Tip:** If you need HR to act on a comment, for example after you sign the RWE, let them know directly through chat or email with the IR number. They won't get an automatic email for it.

---

## 4. Quick reference by role

**QA / RTA / Trainer / IT (filer)**
- File the IR with clear facts and attachments.
- Follow the IR through the emails and add comments when needed.
- Status changes (Request RWE, signing, disciplinary action, waiving) belong to the agent's TL/SOM and HR. You can comment, but you can't change the status.

**TL / SOM**
- **Pending:** decide whether to request an RWE, resolve, or waive.
- **RWE for Signature:** sign, then mark *RWE for Service*.
- **Forwarded:** decide whether to proceed with disciplinary action or waive.
- At every other stage you're waiting on HR. Comment if you need to.

**HR**
- **RWE Request:** prepare the document, then mark *RWE for Signature* (or *IR Reviewed*).
- **RWE for Service:** serve it to the agent, then mark *RWE Served*.
- **Awaiting Explanation:** collect the explanation, then mark *Explanation Received*.
- **For Memo:** write the memo, then mark *Resolved*.

---

## 5. FAQ

**I made a mistake in my IR. Can I fix it?**
Yes. The person who filed the IR can edit the date, agent and summary from the report page. Every edit is saved in the edit history.

**Can I edit my comment?**
Yes, you can edit your own comments. Edits are tracked too.

**The agent's TL didn't get the email.**
The TL is found from the supervisor mapping. If the agent has no TL assigned there, no TL email goes out. Ask HR or the portal admin to update the mapping.

**Does the agent get notified?**
No. The agent isn't emailed by the system. HR serves the RWE to them directly.
