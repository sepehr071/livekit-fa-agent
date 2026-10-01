// Captures README screenshots of client/index.html with a SIMULATED LiveKit SDK (demo data).
// No SFU, token or agent is needed: the SDK file is replaced via Playwright page.route.
//
//   python -m http.server 4100 --bind 127.0.0.1 --directory client
//   node scripts/capture-screenshots.js            # needs `playwright` resolvable (NODE_PATH ok)
const { chromium } = require('playwright');
const path = require('path');

const BASE = process.env.BASE_URL || 'http://127.0.0.1:4100/';
const OUT = path.join(__dirname, '..', 'docs', 'images');

// Fake window.LivekitClient: mode "connecting" never finishes; mode "connected" walks the
// room through connect -> agent join -> audio -> an ICE/TCP selected candidate pair.
const fakeSdk = (mode) => `
window.LivekitClient = {
  RoomEvent:{ConnectionStateChanged:'csc',Disconnected:'dis',ParticipantConnected:'pc',TrackSubscribed:'ts'},
  Room: class {
    constructor(){ this.h={}; this.name='rahnama-demo'; this.localParticipant={setMicrophoneEnabled:async()=>{}}; }
    on(e,f){ this.h[e]=f; return this; }
    emit(e,...a){ this.h[e]&&this.h[e](...a); }
    async connect(){
      this.emit('csc','connecting');
      if(${JSON.stringify(mode)}==='connecting') return new Promise(()=>{});
      await new Promise(r=>setTimeout(r,300));
      this.emit('csc','connected');
      const stats=new Map([
        ['p1',{id:'p1',type:'candidate-pair',nominated:true,state:'succeeded',localCandidateId:'l1'}],
        ['l1',{id:'l1',type:'local-candidate',protocol:'tcp',candidateType:'host'}]]);
      this.engine={pcManager:{subscriber:{pc:{getStats:async()=>stats}}}};
      setTimeout(()=>{ this.emit('pc',{identity:'agent-rahnama'});
        this.emit('ts',{kind:'audio',attach:()=>document.createElement('audio')},{},{identity:'agent-rahnama'}); },200);
    }
  }
};`;

const shots = [
  { name: 'client-idle-light.png', scheme: 'light', mode: null, token: false },
  { name: 'client-connecting-dark.png', scheme: 'dark', mode: 'connecting', token: true },
  { name: 'client-connected-light.png', scheme: 'light', mode: 'connected', token: true },
  { name: 'client-connected-dark.png', scheme: 'dark', mode: 'connected', token: true },
  { name: 'client-mobile-dark.png', scheme: 'dark', mode: 'connected', token: true, w: 390, h: 844, full: true },
];

(async () => {
  const browser = await chromium.launch();
  for (const s of shots) {
    const ctx = await browser.newContext({ viewport: { width: s.w || 1440, height: s.h || 900 },
      deviceScaleFactor: 2, colorScheme: s.scheme });
    const page = await ctx.newPage();
    await page.route('**/livekit-client.umd.min.js', (r) =>
      r.fulfill({ contentType: 'text/javascript', body: fakeSdk(s.mode) }));
    const qs = s.token ? '?token=demo-token&url=wss://lk.example.com' : '';
    await page.goto(BASE + qs, { waitUntil: 'networkidle' });
    await page.evaluate(() => document.fonts.ready);
    if (s.mode) { await page.click('#go'); await page.waitForTimeout(s.mode === 'connecting' ? 700 : 2600); }
    await page.screenshot({ path: path.join(OUT, s.name), fullPage: !!s.full });
    console.log('saved', s.name);
    await ctx.close();
  }
  // Hero banner: frames a real screenshot next to the pipeline (scripts/hero.html).
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 640 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  await page.goto('file://' + path.join(__dirname, 'hero.html').replace(/\\/g, '/'), { waitUntil: 'networkidle' });
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(OUT, 'hero.png') });
  console.log('saved hero.png');
  await browser.close();
})();
