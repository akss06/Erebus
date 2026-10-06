# Erébus — Round 2 Plan

**Submission deadline: 8 Oct 2026 — FINAL, no buffer** (organisers confirmed by email; ignore the 11 Oct date).
**Mentorship (virtual): 6–9 Oct, all four members must attend** (slot may fall on any day).
**Jury: 14 Oct, in person, Delhi, max 2 members attend; travel + stay arranged by TOI.** The two attendees' names were sent on 2 Oct.
Stack: React frontend + FastAPI backend. Engineering: Akshat. Teammates: deck, video, labelling.
Status (3 Oct, evening): done = credentials check, GFW validation, confidence score, web app skeleton, Gulf of Mannar candidates, AIS-off simulation (see WORKLOG.md). Remaining: scale-up of the dark search, offline basemap, deploy, video, deck, Q&A.

## Rules we hold ourselves to

- Never overstate. Every item on the site carries a provenance label: **Real detection**, **Simulated**, or **Unverified candidate**.
- A "dark vessel" is only called that if it is a candidate pending verification. A zero-result is reported as a zero-result.
- Simulated demos are always labelled as simulations.
- Secrets (Earth Engine key, GFW token) live in env vars on the host, never in the repo.

## Goal of the prototype

A real website judges can click through that shows: (1) it finds ships, (2) it flags ships not broadcasting AIS, (3) how reliable it is (numbers + known failure modes).

## Checklist

### Oct 1 (today) — checks
- [ ] Verify Earth Engine still works (non-commercial Cloud project registered)
- [ ] Verify GFW API token still works
- [ ] Query Sentinel-1 pass dates for: Palk Strait / IMBL, Gulf of Mannar offshore, one open-ocean box
- [ ] Get the rest of the Round 2 email: how is the prototype submitted? video required?
- [x] Delhi travel: TOI arranges it; 2 attendees named (done 2 Oct)

### Oct 2 — validation + backend skeleton
- [ ] Check GFW SAR-detection dataset (believed `public-global-sar-presence`) exists and is queryable
- [ ] Run our detector on scenes with GFW labels; compute found / missed / false alarms
- [ ] FastAPI skeleton: AOIs, scenes, detections, reviews; load the existing 4-date Tuticorin data
- [ ] Fix known Round 1 inconsistency: detection #1 is NEREUS PROGRESS, not "unexplained"

### Oct 3–4 — real search + scoring
- [ ] Tiled 10 m runs on Palk Strait / IMBL (avoid the Earth Engine size cap)
- [ ] Persistence logic: object at same spot on every pass = fixed structure, not a ship
- [ ] Automatic confidence score replacing hand-labelled `visual_class`
- [ ] Review every unmatched bright candidate by eye; record outcome honestly
- [ ] React skeleton: map, date slider, detection detail panel

### Oct 5 — first mentor-ready build
- [ ] Review queue (confirm / reject candidate), report export (GeoJSON/PDF)
- [ ] Provenance labels on every item
- [ ] AIS-off experiment: remove NEREUS PROGRESS's AIS, show the system flag it (labelled simulation)
- [ ] Deploy (frontend + API); smoke test the public URL

### Oct 6 — mentorship begins
- [ ] Demo to mentors, collect feedback, ask for introductions (Coast Guard / fisheries / ISRO)
- [ ] Merge teammates' ship / not-ship labels; recompute metrics
- [ ] Optional: synthetic-target injection test for recall by ship size

### Oct 7 — freeze
- [ ] Final metrics + limitations page
- [ ] Code freeze, final deploy, full click-through test
- [ ] Record backup demo video
- [ ] Confirm app also runs locally (offline fallback for Delhi)

### Oct 8 — submit
- [ ] Submit prototype (link/upload per the email)

### Oct 9–13
- [ ] Fix only serious issues
- [ ] Rehearse 5-min pitch + Q&A
- [ ] Carry: demo laptop with local copy, phone hotspot, video offline, printed one-pager
- [ ] The 2 attendees travel per TOI's itinerary (expect the 13th)

### Stretch (only if core is done)
- [ ] NISAR feasibility: is there a scene over our AOI since 17 Jun 2026? (one afternoon max)
- [ ] Small ML classifier (vessel vs clutter), keep only if it beats the rules on held-out data
- [ ] Async "run a new AOI/date" job

## Likely jury questions to prepare

- Why not buy Spire / HawkEye?
- What is your recall on small boats?
- Isn't free AIS too coarse? (yes; needs Coast Guard / NIC feed)
- Did you actually find a dark vessel? (answer exactly as the data says)
- What would you do with 6 more months / who is the first user?
