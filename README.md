SNAPPIC WEBHOOK DASHBOARD

(or: "the little SQLite gremlin that watches your photo booth so you don't have to")


Somewhere, right now, a stranger is making an incredible face into an
iPad/DSLR at a wedding/corp event/shindig. This app is the thing
standing backstage catching every single one of those moments the
INSTANT they happen — sessions, shares, surveys, prize wins — and
throwing them onto a live dashboard like a caffeinated carnival barker
yelling "STEP RIGHT UP, WE GOT A WINNER."

No polling. No refreshing like a caveman. WebSockets, baby. It just
appears.


WHAT THIS THING ACTUALLY DOES


Catches all 4 Snappic webhook types (session / share / survey / competition_win) and does NOT drop the ball
Checks the X-Signature HMAC so randos on the internet can't gleem fake data at your dashboard
Hoards everything in a tiny SQLite file like a raccoon with a shiny-objects collection
Blasts new events to the dashboard live over WebSocket
Does the math for you (avg survey rating, share channels, prize win count) so you don't have to open Excel like some kind of animal



HOW TO BRING IT TO LIFE

Step 1 — feed it dependencies

bashpython3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

Step 2 — give it the secret handshake

Copy .env.example → .env, drop in the secret key from Snappic's
Webhooks settings page. This is the thing that proves incoming data is
actually from Snappic and not some chaos gremlin poking your public URL.

bashexport SNAPPIC_WEBHOOK_SECRET="your-secret-here"

Do NOT commit this. .gitignore already has your back, but worth
saying out loud: secrets live on the server, not on GitHub, we're not
animals.

Step 3 — wake it up

bashuvicorn main:app --host 0.0.0.0 --port 8000

Step 4 — poke it yourself before poking the real thing

No ngrok, no Snappic account, no problem — there's a script that fires
fake events at your local server so you can watch the flash animation
go brrr like an A10 Warthog:

bashpython test_webhook.py

Step 5 — build the tunnel to the outside world

bashngrok http 8000

Grab the URL it spits out, staple /webhook/snappic onto the end, hand
that whole thing to Snappic:

https://your-tunnel-here.ngrok-free.app/webhook/snappic

Hit Verify, hit Send Test, watch it land on your dashboard like
a coin dropping into a claw machine.

Step 6 — show off

The dashboard itself (no /webhook/snappic needed) is your public link
if you want someone to watch the chaos in real time:

https://your-tunnel-here.ngrok-free.app


KNOWN GREMLINS


still and gif show up as images, video gets an actual
<video> player with controls — no more staring at a broken image
icon wondering where your booth footage went
The dashboard has zero login. Anyone with the link sees it —
including guest emails from shares/surveys. Great for a demo, not
great for leaving open at a real event indefinitely
Snappic retries failed webhooks 3x, 10s apart — the app always 200s
once it's parsed and saved, so duplicates should be rare, not extinct
Free ngrok URLs reroll every restart — treat it like a carnival
ticket, not a permanent address



built for catching chaos in real time. no ads, no tracking, just your
friendly neighbourhood webhooks.
[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/B7K4239XFM)
