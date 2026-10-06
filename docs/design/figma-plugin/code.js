// Generated from ../onboarding_screens_4-7.figma.js (the same script, wrapped as a plugin).
// Figma desktop: Plugins > Development > Import plugin from manifest... > pick manifest.json, then run it
// with the SAINT — Onboarding file open on its Onboarding page.
(async () => {
  // Figma Plugin API script (run with the Figma MCP `use_figma` tool) for the SAINT onboarding file
  // https://www.figma.com/design/XCpQUI3TpVjhwUtq4SoZzH — finishes what the 2026-10-05 session couldn't
  // (the Starter plan's MCP call limit): fixes the Ok/Warn chip tint and adds screens 4–7.
  // Uses the file's own variables (collection "SAINT"), text styles (SAINT/*) and components; IDs below are
  // from that file. See docs/design/ONBOARDING.md for the flow.

  const VID = {"bg":"VariableID:2:4","sidebar":"VariableID:2:5","surface":"VariableID:2:6","surface-2":"VariableID:2:7","raised":"VariableID:2:8","border":"VariableID:2:9","border-strong":"VariableID:2:10","text":"VariableID:2:11","muted":"VariableID:2:12","faint":"VariableID:2:13","accent":"VariableID:2:14","accent-soft":"VariableID:2:15","on-accent":"VariableID:2:16","success":"VariableID:2:17","warning":"VariableID:2:18","radius-card":"VariableID:2:21","radius-control":"VariableID:2:22"};
  const V = {}; await Promise.all(Object.entries(VID).map(async ([k, id]) => { V[k] = await figma.variables.getVariableByIdAsync(id); }));
  const TS = {"Display":"S:cbe50a14e203db4b04c77fcf7e647d8ad26ddb36,","Title":"S:5f5a0f631cd0ecfc96e295de1ad7ccf6ea93a2a1,","Heading":"S:f831336ba5200c476dd2824e68678422a9b996b3,","Body":"S:2fac3ef4c50eace944313c8f1ad215890311cf0d,","Body Medium":"S:bae25cbe9fe3d54307a0334f6fe07d5931e69f44,","Caption":"S:37c6eaabc9d6025f49d210b6e515ec7ac3565797,","Eyebrow":"S:22f1bd4501a330c404d50ef9d0e20975454da1f2,","Mono":"S:992341f07a8992b1b52bd22ce2c6c127e8e1627e,","Chip":"S:3b817a0e11ed2970f49515ad47394b8bb6375417,"};
  await Promise.all([["Inter","Regular"],["Inter","Medium"],["Inter","Semi Bold"],["Inter","Bold"],["Cascadia Mono","Regular"]].map(([family, style]) => figma.loadFontAsync({family, style})));
  const paint = (v, opacity) => figma.variables.setBoundVariableForPaint({type: "SOLID", color: {r: 0, g: 0, b: 0}, ...(opacity === undefined ? {} : {opacity})}, "color", v);
  const radius = (n, v) => { for (const k of ["topLeftRadius","topRightRadius","bottomLeftRadius","bottomRightRadius"]) n.setBoundVariable(k, v); };
  async function txt(chars, style, color, width) {
    const t = figma.createText(); await t.setTextStyleIdAsync(TS[style]); t.characters = chars; t.fills = [paint(V[color])];
    if (width) { t.resize(width, t.height); t.textAutoResize = "HEIGHT"; }
    return t;
  }
  const [buttonSet, chipSet, stepSet, cardSet, rowSet, svc] = await Promise.all(["3:104","3:113","3:139","3:163","3:182","3:183"].map(id => figma.getNodeByIdAsync(id)));
  const ICON = {"mic":"3:8","volume":"3:13","zap":"3:16","check":"3:19","sparkles":"3:22","smartphone":"3:26","music":"3:31","cpu":"3:36","bell":"3:40","memory":"3:45","keyboard":"3:49","user":"3:53","globe":"3:58","layout":"3:62","halo":"3:68","search":"3:72","drive":"3:78","alert":"3:83","power":"3:87","widget":"3:92","download":"3:97"};
  const icons = {}; await Promise.all(Object.entries(ICON).map(async ([k, id]) => { icons[k] = await figma.getNodeByIdAsync(id); }));

  // Chip tint: the 12 % fill on a variable-bound paint rendered solid, hiding the label. Plain tinted paints.
  const hex = h => ({r: parseInt(h.slice(1, 3), 16) / 255, g: parseInt(h.slice(3, 5), 16) / 255, b: parseInt(h.slice(5, 7), 16) / 255});
  for (const c of chipSet.children) {
    const tone = {"Tone=Ok": "#4cc38a", "Tone=Warn": "#f5b544"}[c.name];
    if (!tone) continue;
    c.fills = [{type: "SOLID", color: hex(tone), opacity: 0.12}];
    c.strokes = [{type: "SOLID", color: hex(tone), opacity: 0.3}];
  }

  const pk = (set, name) => Object.keys(set.componentPropertyDefinitions).find(k => k.split("#")[0] === name);
  const variant = (set, name) => set.children.find(c => c.name === name);
  const button = (kind, label) => { const i = variant(buttonSet, `Kind=${kind}`).createInstance(); i.setProperties({[pk(buttonSet, "Label")]: label}); return i; };
  const chip = (tone, label) => { const i = variant(chipSet, `Tone=${tone}`).createInstance(); i.setProperties({[pk(chipSet, "Label")]: label}); return i; };
  const card = (title, desc, icon, selected = false, radio = true) => {
    const i = variant(cardSet, `Selected=${selected}`).createInstance();
    i.setProperties({[pk(cardSet, "Title")]: title, [pk(cardSet, "Description")]: desc, [pk(cardSet, "Icon")]: icons[icon].id});
    if (!radio) i.findOne(n => n.name === "Radio").visible = false;
    return i;
  };
  const row = (title, desc, icon, on) => { const i = variant(rowSet, `On=${on}`).createInstance(); i.setProperties({[pk(rowSet, "Title")]: title, [pk(rowSet, "Description")]: desc, [pk(rowSet, "Icon")]: icons[icon].id}); return i; };
  function service(title, desc, icon, tone, status, kind, action) {
    const i = svc.createInstance();
    i.setProperties({[pk(svc, "Title")]: title, [pk(svc, "Description")]: desc, [pk(svc, "Icon")]: icons[icon].id});
    const s = i.findOne(n => n.name === "Status"); s.setProperties({Tone: tone, [pk(chipSet, "Label")]: status});
    const b = i.findOne(n => n.name === "Action"); b.setProperties({Kind: kind, [pk(buttonSet, "Label")]: action});
    return i;
  }
  const auto = (dir, name, props = {}) => { const f = figma.createAutoLayout(dir, {name, ...props}); f.fills = []; return f; };
  const fill = (parent, child, h = "FILL", v) => { parent.appendChild(child); if (h) child.layoutSizingHorizontal = h; if (v) child.layoutSizingVertical = v; return child; };
  const grow = (parent) => { const s = figma.createFrame(); s.name = "Spacer"; s.fills = []; s.resize(1, 1); parent.appendChild(s); s.layoutGrow = 1; return s; };
  const gapOf = (h) => { const g = figma.createFrame(); g.name = "Gap"; g.fills = []; g.resize(10, h); return g; };
  const panel = (name, dir = "VERTICAL") => { const p = figma.createAutoLayout(dir, {name}); p.fills = [paint(V.surface)]; p.strokes = [paint(V.border)]; radius(p, V["radius-card"]); p.paddingLeft = p.paddingRight = p.paddingTop = p.paddingBottom = 20; p.itemSpacing = 14; return p; };

  // Screens 1–3 already exist: reuse screen 2's rail and footer structure by cloning it, then swap the content.
  const template = await figma.getNodeByIdAsync("4:149");
  const STEPS = [["Welcome", "Meet SAINT"], ["Microphone", "Wake word & mic"], ["Voice", "How SAINT sounds"], ["Your apps", "Names SAINT knows"], ["Connect", "Spotify, AI, phone"], ["Safety", "When SAINT asks first"], ["Ready", "Try a command"]];
  async function shell(n, title, desc) {
    const screen = template.clone(); figma.currentPage.appendChild(screen);
    screen.name = `${String(n).padStart(2, "0")} ${STEPS[n - 1][0]}`; screen.x = (n - 1) * 1560; screen.y = 0;
    screen.findAll(c => c.type === "INSTANCE" && c.mainComponent && c.mainComponent.parent && c.mainComponent.parent.id === stepSet.id)
      .forEach((s, i) => s.setProperties({State: i + 1 < n ? "Done" : i + 1 === n ? "Current" : "Upcoming"}));
    const head = screen.findOne(c => c.name === "Header");
    const [eb, ti, de] = head.findAll(c => c.type === "TEXT");
    eb.characters = `Step ${n} of 7`; ti.characters = title; de.characters = desc;
    const content = screen.findOne(c => c.name === "Content");
    for (const c of [...content.children]) c.remove();
    const footer = screen.findOne(c => c.name === "Footer");
    for (const c of [...footer.children]) c.remove();
    return {screen, content, footer};
  }
  function footerButtons(footer, back, skip, primary, secondary) {
    if (back) footer.appendChild(button("Ghost", back));
    grow(footer);
    if (skip) footer.appendChild(button("Ghost", skip));
    if (secondary) footer.appendChild(button("Secondary", secondary));
    footer.appendChild(button("Primary", primary));
  }
  const created = [];

  // ---------- 04 Your apps ----------
  {
    const {screen, content, footer} = await shell(4, "SAINT found your apps and games",
      "These are the names SAINT listens for. Speech-to-text often mishears names it doesn't know — SAINT matches them by sound, asks when two apps sound alike, and remembers your answer.");
    const apps = panel("Apps");
    const ah = auto("HORIZONTAL", "Head", {itemSpacing: 12, counterAxisAlignItems: "CENTER"});
    ah.appendChild(await txt("186 apps · 9 games", "Heading", "text")); grow(ah);
    ah.appendChild(chip("Neutral", "Start menu · Store · Steam · shortcuts")); ah.appendChild(button("Secondary", "Rescan"));
    fill(apps, ah);
    const grid = auto("HORIZONTAL", "App names", {itemSpacing: 8}); grid.layoutWrap = "WRAP"; grid.counterAxisSpacing = 8;
    for (const n of ["Claude", "Discord", "Spotify", "Steam", "Rocket League", "Opera", "Visual Studio Code", "Notion", "OBS Studio",
                     "Blender", "Fortnite", "Bloxstrap", "Docker Desktop", "Unity Hub", "FL Studio", "iCloud", "Clock", "+ 169 more"])
      grid.appendChild(chip(n === "+ 169 more" ? "Accent" : "Neutral", n));
    fill(apps, grid);
    fill(content, apps);
    const heard = panel("Misheard names");
    heard.appendChild(await txt("Names it has learned to hear", "Heading", "text"));
    for (const [said, meant, how] of [["“clad”, “clawed”", "Claude", "by sound"], ["“rocket leak”", "Rocket League", "by sound"],
                                      ["“block strap”", "Bloxstrap", "by sound"], ["“cloud”", "Claude", "you picked it over iCloud"]]) {
      const r = auto("HORIZONTAL", "Heard row", {itemSpacing: 12, counterAxisAlignItems: "CENTER"});
      r.appendChild(await txt(said, "Mono", "muted"));
      r.appendChild(await txt("→", "Body", "faint"));
      r.appendChild(await txt(meant, "Body Medium", "text"));
      grow(r);
      r.appendChild(chip(how.startsWith("you") ? "Accent" : "Neutral", how));
      fill(heard, r);
    }
    fill(heard, await txt("Nothing is added to the speech model: SAINT compares what it heard with the names on this PC.", "Caption", "faint", 600));
    fill(content, heard);
    footerButtons(footer, "Back", "Skip for now", "Continue");
    created.push(screen.id);
  }

  // ---------- 05 Connect ----------
  {
    const {screen, content, footer} = await shell(5, "Connect what you use",
      "All optional — you can connect these later in Settings. SAINT works without any of them.");
    fill(content, service("Spotify", "Play, skip, queue and “skip 3 songs” by voice. New queues replace SAINT's old ones.", "music", "Neutral", "Not connected", "Primary", "Connect"));
    fill(content, service("AI model", "Ollama on this PC answers questions and plans tasks. Nothing leaves the PC.", "cpu", "Ok", "Running · llama3.1", "Secondary", "Change"));
    fill(content, service("Your iPhone", "SAINT Link: talk to your PC's SAINT from your phone, privately encrypted.", "smartphone", "Neutral", "Optional", "Secondary", "Pair"));
    fill(content, service("Another PC", "Share what SAINT learns between your computers.", "globe", "Neutral", "Optional", "Secondary", "Pair"));
    footerButtons(footer, "Back", "Skip for now", "Continue");
    created.push(screen.id);
  }

  // ---------- 06 Safety ----------
  {
    const {screen, content, footer} = await shell(6, "Decide when SAINT asks first",
      "SAINT asks questions before it acts on a task it doesn't fully understand — which account, who it's to, what to say — and never sends, deletes or closes things without your yes.");
    const cols = auto("HORIZONTAL", "Columns", {itemSpacing: 24});
    const left = auto("VERTICAL", "Rules", {itemSpacing: 12});
    fill(left, row("Ask before sending emails and messages", "Drafts are read back for changes; Send waits for your yes.", "sparkles", true));
    fill(left, row("Ask before closing apps", "“Close Discord?” — a plain yes or no answers it.", "power", true));
    fill(left, row("Ask before deleting files", "Clean-ups list what they found first.", "drive", true));
    fill(left, row("Show what SAINT is doing", "A small notice for each action, so nothing happens silently.", "bell", true));
    fill(left, row("Click and type inside games", "Off: anti-cheat can treat it as a bot.", "widget", false));
    fill(cols, left, "FILL");
    const demo = panel("Example");
    demo.fills = [paint(V.sidebar)];
    demo.appendChild(await txt("Example · “write me an email”", "Eyebrow", "faint"));
    const say = async (who, text, mine) => {
      const b = auto("VERTICAL", who, {itemSpacing: 4}); b.paddingLeft = b.paddingRight = 14; b.paddingTop = b.paddingBottom = 10;
      radius(b, V["radius-control"]); b.fills = [paint(mine ? V["surface-2"] : V.surface)]; if (!mine) b.strokes = [paint(V.border)];
      if (!mine) b.appendChild(await txt("SAINT", "Eyebrow", "accent"));
      b.appendChild(await txt(text, "Body", "text", 300));
      return b;
    };
    demo.appendChild(await say("You", "Write me an email to Sam about Friday.", true));
    demo.appendChild(await say("SAINT", "Before I start — which email should I send from, personal or work?", false));
    demo.appendChild(await say("You", "Personal.", true));
    demo.appendChild(await say("SAINT", "Here's the subject: “Plans for Friday”. Should I use it, or what should I change?", false));
    demo.appendChild(await say("SAINT", "Should I send it?", false));
    cols.appendChild(demo); demo.resize(380, demo.height); demo.layoutSizingHorizontal = "FIXED";
    fill(content, cols);
    footerButtons(footer, "Back", null, "Continue");
    created.push(screen.id);
  }

  // ---------- 07 Ready ----------
  {
    const {screen, content, footer} = await shell(7, "You're set. Try saying…",
      "Say “Hey SAINT” first, or just talk while SAINT is listening for a follow-up. Press Alt+` any time to open the overlay.");
    const grid = auto("HORIZONTAL", "Try it", {itemSpacing: 16}); grid.layoutWrap = "WRAP"; grid.counterAxisSpacing = 16;
    for (const [t, d, i] of [["“Open Claude”", "Opens it — or switches to it if it's already open.", "zap"],
                             ["“Play something chill”", "A queue from your own listening, not a random song.", "music"],
                             ["“Write me an email to Sam about Friday”", "Asks what it needs, drafts it, waits before Send.", "sparkles"],
                             ["“Skip 3 songs”", "Or “skip”, “pause”, “louder” — no wake word while music plays.", "music"],
                             ["“Talk louder”", "SAINT's own voice volume. Spotify and Windows stay put.", "volume"],
                             ["“Gaming mode on”", "Moves SAINT off the game's screen and keeps the mini player.", "widget"]])
      grid.appendChild(card(t, d, i, false, false));
    fill(content, grid);
    footerButtons(footer, "Back", null, "Open SAINT", "Watch the demo");
    created.push(screen.id);
  }
  return { createdScreenIds: created };
})().then(r => figma.closePlugin("Added screens 4–7: " + JSON.stringify(r)),
          e => figma.closePlugin("Failed: " + (e && e.message || e)));
