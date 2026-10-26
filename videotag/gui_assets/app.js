"use strict";

// Everything on the page is built with el(): text goes in as text nodes, never as HTML, because descriptions come
// from a model and can be edited by anyone with access to Photos.

const TOKEN = document.querySelector('meta[name="videotag-token"]').content;
const PAGE_SIZE = 100;
const API_VERSION = 4;  // must equal API_VERSION in gui.py: an update changes this page at once, but an open dashboard keeps its old code

const app = {
  status: null, library: null, history: null, version: null, settings: null,
  accuracy: null, spot: null, accountGeneration: 0, dataGeneration: 0,
  filter: {text: "", result: "all", person: "all"}, shown: PAGE_SIZE, open: new Set(), tab: "overview",
};

// -- helpers ------------------------------------------------------------------------------------

function el(tag, props, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "style") node.style.cssText = value;  // CSSOM, not setAttribute: the CSP forbids inline style attributes
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

const $ = (id) => document.getElementById(id);

function fill(node, ...kids) {
  node.replaceChildren(...kids.flat(Infinity).filter((kid) => kid !== null && kid !== undefined && kid !== false));
}
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function duration(seconds) {
  if (seconds === null || seconds === undefined) return "";
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return seconds % 60 ? `${minutes} min ${seconds % 60} s` : `${minutes} min`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

function when(iso) {
  const date = new Date(iso);
  const time = date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  const days = Math.round((new Date().setHours(0, 0, 0, 0) - new Date(date).setHours(0, 0, 0, 0)) / 86400000);
  if (days === 0) return `today at ${time}`;
  if (days === 1) return `yesterday at ${time}`;
  return `${date.toLocaleDateString([], {day: "numeric", month: "short"})} at ${time}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {"X-Videotag-Token": TOKEN, "Content-Type": "application/json"}});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw Object.assign(new Error(data.error || response.statusText), {status: response.status});
  return data;
}

let toastTimer;
function toast(message) {
  const box = $("toast");
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 6000);
}

// -- opening a video in Photos ---------------------------------------------------------------------

function photosLink(video) {
  const account = app.account;  // results of another account would link into the wrong library, so only the current one's do
  if (!account || !account.current || !account.owner || account.mismatch || !video.folder_id) return null;
  return `${account.current.url}/?launchApp=SYNO.Foto.AppInstance#/personal_space/folder/${video.folder_id}/item/${video.id}`;
}

function videoTitle(video) {
  const href = photosLink(video);
  return href ? el("a", {href, target: "_blank", rel: "noopener noreferrer", title: "Open this video in Synology Photos",
                         onclick: (event) => event.stopPropagation()}, video.filename)  // a click on the name must not fold the row
              : video.filename;
}

// -- vocabulary: how the data's words read to a person -------------------------------------------

const RESULTS = [
  ["auto", "Confident", "Enough frames match a named person. Tagged with the plain name."],
  ["review", "Suggestion", "Some frames match. Tagged with a question mark so you can confirm it."],
  ["none", "No match", "No named person was recognised."],
  ["error", "Failed", "The video could not be analysed. The log says why."],
];
const RESULT_LABEL = Object.fromEntries(RESULTS.map(([key, label]) => [key, label]));
const PERSON_STATUS = {
  tagged: "Tagged in Photos", suggested: "Suggested in Photos", planned: "Not written yet",
  confirmed: "Confirmed by you", rejected: "Removed by you",
};
const DESCRIPTION_STATUS = {
  written: "in Photos", rejected: "removed by you", pending: "not written yet", none: "none",
};

const PRESET_NAMES = {thorough: "Thorough", balanced: "Balanced", fast: "Fast"};

function ruleText(preset) {
  if (!preset.tail_fps) return `${preset.fps} frames a second throughout`;
  return `${preset.fps} frames a second for the first ${preset.head_seconds} s, then one every ${1 / preset.tail_fps} s`;
}

function savingText(name, estimate) {
  if (name === "thorough") return "Today's behaviour, and the default.";
  const saving = estimate.saving[name];
  return saving === null ? "Analyse some videos first to see what this would save on your library."
    : `About ${saving}% fewer frames on your ${plural(estimate.videos, "analysed video")}.`;
}

function personChip(person) {
  const label = {tagged: person.name, suggested: `${person.name}?`, planned: `○ ${person.name}`,
                 confirmed: `✓ ${person.name}`, rejected: `✕ ${person.name}`}[person.status] || person.name;
  return el("span", {class: `chip ${person.status}`, title: `${person.name}: ${PERSON_STATUS[person.status] || person.decision}`}, label);
}

// -- the run strip -------------------------------------------------------------------------------

function undoCopy(state, run) {
  if (state === "running") {
    return run.finished ? {title: "Finishing up", lead: "Everything is removed and the undo is closing."}
      : {title: run.phase, lead: "Taking the tags, person links, descriptions and albums that videotag created out of Photos. Yours are left alone."};
  }
  if (state === "interrupted") {
    return {title: "The last undo stopped before it finished",
            lead: `It started ${when(run.started)}. The log shows why. Run it again to remove what is left.`};
  }
  const w = run.written || {};
  return {title: `Last undo finished ${when(run.ended)}`,
          lead: `Removed from Photos: ${plural(w.tags || 0, "tag")}, ${plural(w.faces || 0, "person link")}, `
              + `${plural(w.descriptions || 0, "description")}, ${plural(w.albums || 0, "album")}.`};
}

function runCopy(state, run, report) {
  if (state === "never" && report) {  // reports exist, but the log has no run marker: it predates this dashboard
    return {title: `Last report saved ${when(report.time)}`,
            lead: "Runs from before the dashboard was installed left no progress details in the log. The next run fills them in."};
  }
  if (state === "never") {
    return {title: "No run recorded yet",
            lead: "Start a dry run to see what videotag would tag. It reads Photos and writes nothing."};
  }
  if (state === "running" && !run) {
    return {title: "Starting a run", lead: "Logging in and reading the library."};
  }
  if (run && run.mode === "undo") return undoCopy(state, run);
  if (state === "running" && run.finished) {  // the report is saved, the process has not exited yet
    return {title: "Finishing up", lead: "The report is saved and the run is closing."};
  }
  if (state === "running" && run.references && !run.total) {
    return {title: run.phase, lead: "Reading the photos of the people you named. Their faces are saved, so the next run only reads photos that are new or changed."};
  }
  if (state === "running") {
    return {title: run.phase, lead: run.mode === "apply" ? "This run writes to Photos once the analysis is done."
                                                           : "Dry run: nothing is written to Photos."};
  }
  if (state === "interrupted") {
    const progress = run.total ? ` and got through ${run.done} of ${run.total} videos` : "";
    return {title: "The last run stopped before it finished",
            lead: `It started ${when(run.started)}${progress}. The log shows why. Finished videos are cached, so the next run continues quickly.`};
  }
  const analysed = `${plural(run.done, "video")} analysed in ${duration(run.elapsed_seconds)}.`;
  if (run.mode === "apply") {
    const w = run.written || {};
    return {title: `Last run finished ${when(run.ended)}`,
            lead: `Applied: ${analysed} Written: ${plural(w.tags || 0, "tag change")}, ${plural(w.faces || 0, "person link")}, `
                + `${plural(w.descriptions || 0, "description")}, ${plural(w.albums || 0, "album change")}.`};
  }
  return {title: `Last run finished ${when(run.ended)}`,
          lead: `Dry run: ${analysed} ${plural(run.planned_tags || 0, "tag change")} planned, nothing written. Start an apply run to write them.`};
}

function renderRun() {
  const {state, run} = app.status.status;
  const copy = runCopy(state, run, app.library && app.library.report);
  const analysing = state === "running" && run && run.total && run.phase === "Analysing videos";
  const finishing = state === "running" && run && run.finished;
  const learning = state === "running" && run && run.references && run.references.total > 0 && !run.total && !run.finished;
  const percent = finishing ? 100 : analysing ? Math.round((run.done / run.total) * 100)
    : learning ? Math.round((run.references.done / run.references.total) * 100) : 0;
  const determinate = analysing || finishing || learning;
  const facts = [];
  if (state === "running" && run) {
    if (learning) {
      facts.push(el("span", {class: "figure"}, el("strong", {}, `${run.references.done} of ${run.references.total}`), " photos"));
      if (run.references.remaining_seconds !== null) facts.push(el("span", {}, `about ${duration(run.references.remaining_seconds)} left`));
    }
    if (run.total) facts.push(el("span", {class: "figure"}, el("strong", {}, `${run.done} of ${run.total}`), " videos",
                                 run.slice_of ? ` (a slice of ${run.slice_of})` : ""));
    if (run.current) facts.push(el("span", {}, "Now: ", el("strong", {}, run.current.replace(/^\d+ /, ""))));
    if (run.remaining_seconds !== null && analysing) facts.push(el("span", {}, `about ${duration(run.remaining_seconds)} left`));
    facts.push(el("span", {}, `running for ${duration(run.elapsed_seconds)}`));
  }
  if (run && run.errors) {
    facts.push(el("span", {}, el("strong", {}, plural(run.errors, "problem")), " logged ",
                  el("button", {type: "button", onclick: () => selectTab("log")}, "Open the log")));
  }
  fill($("run"), 
    el("h2", {class: "run-title"}, copy.title),
    el("p", {class: "run-lead"}, copy.lead),
    state === "running" ? el("div", {class: `progress${determinate ? "" : " waiting"}`, role: "progressbar",
                                     "aria-label": "Run progress", "aria-valuemin": 0, "aria-valuemax": 100,
                                     "aria-valuenow": determinate ? percent : null},
                             el("div", {style: determinate ? `width:${percent}%` : null})) : null,
    facts.length ? el("div", {class: "facts"}, facts) : null);

  const badge = $("state-badge");
  badge.className = `badge ${state}`;
  badge.textContent = {running: "Running", idle: "Idle", interrupted: "Stopped early",
                       never: app.library && app.library.report ? "Idle" : "No run yet"}[state];
  $("run-open").disabled = $("undo-open").disabled = state === "running";
  $("run-open").textContent = state === "running" ? "Run in progress" : "Start a run";
  document.title = state === "running" && analysing ? `videotag: ${run.done}/${run.total}`
    : learning ? `videotag: ${run.references.done}/${run.references.total} photos` : "videotag";
}

// -- overview ------------------------------------------------------------------------------------

function segments(parts, className) {
  const total = parts.reduce((sum, [, value]) => sum + value, 0);
  if (!total) return el("div", {class: `${className} empty`});
  return el("div", {class: className, role: "img",
                    "aria-label": parts.map(([label, value]) => `${label}: ${value}`).join(", ")},
            parts.filter(([, value]) => value).map(([label, value, color]) =>
              el("div", {style: `flex:${value} 1 0;background:${color}`, title: `${label}: ${value} of ${total}`})));
}

const RESULT_COLORS = {auto: "var(--c-auto)", review: "var(--c-review)", none: "var(--c-none)", error: "var(--c-error)"};

function factList(rows) {
  return el("dl", {class: "facts-list"}, rows.map(([label, value]) => [el("dt", {}, label), el("dd", {}, value)]));
}

function liveNote() {
  return app.library.report && app.library.report.live
    ? el("p", {class: "notice live"}, "Live view: the results of the run in progress, as they are made. When it finishes, its report takes over.")
    : null;
}

// -- how far the matching can be trusted ---------------------------------------------------------

const KIND_LABEL = {single: "from photos of one person", group: "from group photos", cover: "from the cover"};

function accuracySection() {
  const a = app.accuracy, number = (value) => (value === null || value === undefined ? "-" : value.toFixed(2));
  const heading = el("h2", {}, "How far the matching can be trusted");
  if (!a) {
    return el("section", {class: "people"}, heading,
      el("p", {class: "sub"}, "Every run works this out from the reference faces it builds. Start a run to see it here."));
  }
  const confident = (p) => (p.confident_allowed ? "Allowed" : "Suggestions only: too few reference faces");
  return el("section", {class: "people"}, heading,
    el("p", {class: "sub"}, `Worked out by the run of ${when(a.time)} from the reference faces of each named person. `
       + "Nothing here needs your NAS or a video."),
    el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Person"), el("th", {class: "num"}, "Reference faces"),
        el("th", {class: "num"}, "Own faces recognised"), el("th", {class: "num"}, "Threshold"), el("th", {}, "Confident tags"))),
      el("tbody", {}, a.people.map((p) => el("tr", {},
        el("td", {}, el("strong", {}, p.name)),
        el("td", {class: "num", title: Object.entries(p.kinds).map(([kind, n]) => `${n} ${KIND_LABEL[kind] || kind}`).join(", ")}, p.references),
        el("td", {class: "num", title: "Each reference face compared with the person's other reference faces"},
           p.recognised.of ? `${p.recognised.n} of ${p.recognised.of}` : "-"),
        el("td", {class: "num", title: `Own faces ${number(p.same_person.mean)} on average; the closest other person reaches ${number(p.others.max)}`}, number(p.threshold)),
        el("td", {}, confident(p)))))),
    a.lookalikes.length
      ? el("div", {class: "group"}, el("h3", {}, "Faces that resemble each other"),
          el("ul", {class: "legend"}, a.lookalikes.map((pair) => el("li", {},
            el("span", {}), el("span", {}, `${pair.person} looks like ${pair.resembles}`,
              el("span", {class: "hint"}, `up to ${number(pair.similarity)}; ${pair.faces} of ${pair.of} of ${pair.person}'s reference faces are above the minimum match level. `
                + "The threshold is raised to keep them apart.")),
            el("span", {class: "num"}, number(pair.similarity))))))
      : el("p", {class: "sub", style: "margin-top:10px"}, "No two named people have faces that resemble each other enough to matter."),
    a.mixed_up.length
      ? el("p", {class: "notice"}, "With the thresholds of that run, these reference faces would be given to somebody else: "
          + a.mixed_up.map((pair) => `${pair.faces} of ${pair.person}'s ${pair.of} to ${pair.taken_for}`).join("; ")
          + ". That happens when a threshold is set by hand (--threshold) and is too low.")
      : null);
}

// -- spot-check ----------------------------------------------------------------------------------

function spotGroup(label, group) {
  if (!group.total) return null;
  return el("div", {class: "spot-group"},
    el("h4", {}, `${label}: ${group.sample.length} of ${group.total}`),
    el("ul", {}, group.sample.map((item) => el("li", {}, el("strong", {}, videoTitle({id: item.video_id, folder_id: item.folder_id, filename: item.filename})), el("span", {class: "small"}, `  (video ${item.video_id})`)))));
}

function renderSpot() {
  const people = app.spot;
  const target = $("spot-result");
  if (!target) return;
  fill(target, people === null ? null : people.length
    ? people.map((person) => el("div", {class: "spot-person"}, el("h3", {}, person.name),
        spotGroup("Confident matches", person.confident), spotGroup("Suggestions", person.suggested)))
    : el("p", {class: "sub", style: "margin-top:10px"}, "Nothing to check: the latest report has no matches you have not already settled."));
}

function spotSection() {
  const count = el("select", {id: "spot-count", "aria-label": "Videos per group"},
    [3, 5, 10].map((n) => el("option", {value: String(n), selected: n === 5}, `${n} per group`)));
  const draw = el("button", {type: "button", onclick: async () => {
    draw.disabled = true;
    const accountGeneration = app.accountGeneration, dataGeneration = app.dataGeneration;
    try {
      const sample = await api("/api/spotcheck", {method: "POST", body: JSON.stringify({count: Number(count.value)})});
      if (accountGeneration !== app.accountGeneration || dataGeneration !== app.dataGeneration) return;
      app.spot = sample;
      renderSpot();
    } catch (error) {
      if (accountGeneration === app.accountGeneration && dataGeneration === app.dataGeneration) toast(error.message);
    } finally { draw.disabled = false; }
  }}, "Draw a sample");
  setTimeout(renderSpot);  // the section is in the page by then; a sample drawn before a refresh stays on screen
  return el("section", {class: "people"},
    el("h2", {}, "Spot-check"),
    el("p", {class: "sub"}, "A random sample of each person's matches in the latest report. Search a file name in Photos to open the video and see whether the match is right. "
       + "Matches you already confirmed or removed in Photos are left out."),
    el("div", {class: "spot-head"}, count, draw),
    el("div", {id: "spot-result"}));
}

function renderOverview() {
  const lib = app.library;
  if (!lib || !lib.report) {
    fill($("view-overview"), 
      el("h2", {}, "Nothing to show yet"),
      el("p", {class: "sub"}, "Every run saves a report of what it found. Start a dry run and the results appear here."));
    return;
  }
  const t = lib.totals, s = lib.state;
  const parts = RESULTS.map(([key, label]) => [label, key === "error" ? t.errors : t[key], RESULT_COLORS[key]]);
  const counts = {auto: t.auto, review: t.review, none: t.none, error: t.errors};
  const legend = el("ul", {class: "legend"}, RESULTS.map(([key, label, hint]) =>
    el("li", {}, el("span", {class: "swatch", style: `background:${RESULT_COLORS[key]}`}),
       el("span", {}, label, el("span", {class: "hint"}, hint)), el("span", {class: "num"}, counts[key]))));

  const people = el("section", {class: "people"},
    el("h2", {}, "People"),
    el("p", {class: "sub"}, "Counted over the videos of the latest report. Your own corrections in Photos are respected and never overwritten."),
    lib.people.length ? el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Person"),
        ["Tagged", "Suggested", "Not written yet", "Confirmed by you", "Removed by you"].map((h) => el("th", {class: "num"}, h)))),
      el("tbody", {}, lib.people.map((p) => el("tr", {}, el("td", {}, el("strong", {}, p.name)),
        [p.tagged, p.suggested, p.planned, p.confirmed, p.rejected].map((n) => el("td", {class: "num"}, n)))))) :
      el("p", {class: "sub"}, "No named people were available to the last run."));

  const twins = lib.twins.length ? el("section", {class: "people"},
    el("h2", {}, "The same file more than once"),
    el("p", {class: "sub"}, `${plural(lib.twins.length, "file")} sit${lib.twins.length === 1 ? "s" : ""} in Photos as more than one video. New copies reuse analysis; existing saved results are kept. `
       + "To tidy up, delete the extra copies in Photos yourself: videotag never deletes a video."),
    el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "File"), el("th", {}, "Video numbers in Photos"))),
      el("tbody", {}, lib.twins.map((t) => el("tr", {}, el("td", {}, el("strong", {}, t.filename)), el("td", {}, t.ids.join(", ")))))))
    : null;

  fill($("view-overview"), 
    lib.problem ? el("p", {class: "notice"}, `${lib.problem}. Showing the report without the state of your tags until it can be read.`) : null,
    liveNote(),
    el("div", {class: "overview"},
      el("div", {},
        el("h2", {}, "Videos by result"),
        el("p", {class: "sub"}, lib.report.live ? `${plural(t.videos, "video")} so far in the run that started ${when(lib.report.time)}`
                                                : `${plural(t.videos, "video")} in the report of ${when(lib.report.time)}`),
        segments(parts, "bar"), legend,
        el("p", {class: "sub", style: "margin-top:14px"}, `${t.described} of ${t.videos} videos have a description.`)),
      el("div", {},
        el("div", {class: "group"}, el("h3", {}, "Written to Photos by videotag"),
          factList([["Confident tags", s.tags_auto], ["Suggestion tags", s.tags_review], ["Person links", s.face_links],
                    ["Descriptions", s.descriptions], ["Albums", s.albums]])),
        el("div", {class: "group"}, el("h3", {}, "Your corrections"),
          factList([["Matches you confirmed", s.confirmed], ["Matches you removed", s.rejected],
                    ["Descriptions you removed", s.descriptions_rejected]])))),
    people, twins, accuracySection(), spotSection());
}

// -- videos --------------------------------------------------------------------------------------

function searchText(video) {
  if (!video.search) {
    const d = video.description;
    video.search = [video.filename, video.people.map((p) => p.name), d && [d.en, d.de, d.topics.en, d.topics.de, d.objects.en,
                    d.objects.de, d.place]].flat(3).filter(Boolean).join(" ").toLowerCase();
  }
  return video.search;
}

function visibleVideos() {
  const f = app.filter, text = f.text.trim().toLowerCase();
  return app.library.videos.filter((v) =>
    (f.result === "all" || v.decision === f.result) &&
    (f.person === "all" || v.people.some((p) => String(p.id) === f.person)) &&
    (!text || searchText(v).includes(text)));
}

function descriptionBlock(video) {
  const d = video.description;
  if (!d) return el("p", {class: "sub"}, "No description. The description model was off for this video, or it gave no usable answer.");
  const chips = (items) => items.map((item) => el("span", {class: "chip plain"}, item));
  return [
    el("div", {class: "langs"},
      el("p", {class: "caption"}, el("span", {class: "tag"}, "English"), d.en),
      el("p", {class: "caption"}, el("span", {class: "tag"}, "Deutsch"), d.de)),
    d.topics.en.length ? el("div", {class: "chips", "aria-label": "Topics"}, chips([...d.topics.en, ...d.topics.de])) : null,
    d.objects.en.length ? el("div", {class: "chips", "aria-label": "Objects"}, chips([...d.objects.en, ...d.objects.de])) : null,
    d.place ? el("p", {class: "small", style: "margin-top:8px"}, `Place: ${d.place}`, d.evidence ? ` (shown by: ${d.evidence})` : "") : null,
  ];
}

function cropRows(video) {
  return video.people.filter((p) => p.crops.length).map((p) => el("div", {class: "crop-row"},
    el("span", {class: "small"}, `${p.name}, the faces it is based on:`),
    p.crops.map((name) => el("img", {class: "crop", src: `/evidence/${video.evidence_id}/${name}`, loading: "lazy",
                                     alt: `A face of ${p.name} that the match is based on`}))));
}

function videoDetail(video) {
  const people = video.people.length ? el("table", {},
    el("thead", {}, el("tr", {}, el("th", {}, "Person"), el("th", {}, "Result"), el("th", {class: "num"}, "Best match"),
                       el("th", {class: "num"}, "Frames"), el("th", {}, "In Photos"))),
    el("tbody", {}, video.people.map((p) => el("tr", {}, el("td", {}, personChip(p)), el("td", {}, RESULT_LABEL[p.decision]),
      el("td", {class: "num"}, p.best === null ? "-" : p.best.toFixed(2)), el("td", {class: "num"}, p.hits),
      el("td", {}, PERSON_STATUS[p.status]))))) : null;
  const checked = video.frames === null ? "" : `Checked ${plural(video.frames, "frame")} and found ${plural(video.faces, "face")}.`;
  return el("div", {class: "detail"},
    el("div", {},
      el("div", {class: "block"}, el("h3", {}, "People"),
        video.error ? el("p", {class: "problem"}, video.error) : people || el("p", {class: "sub"}, "No named person was recognised."),
        cropRows(video),
        video.twin_of ? el("p", {class: "small", style: "margin-top:6px"}, `The same file as video ${video.twin_of}: it was analysed once and this video shares the result.`) : null,
        checked ? el("p", {class: "small", style: "margin-top:6px"}, checked) : null)),
    el("div", {},
      el("div", {class: "block"}, el("h3", {}, "What the model saw"),
        descriptionBlock(video))));
}

function videoRow(video) {
  const who = video.error ? [el("span", {class: "chip error"}, "Failed")]
    : video.people.length ? video.people.map(personChip) : [el("span", {class: "chip plain"}, "No match")];
  const row = el("details", {class: "video", "data-id": video.id},
    el("summary", {},
      el("span", {class: `strip ${video.decision}`, title: RESULT_LABEL[video.decision]}),
      el("div", {}, el("div", {class: "name"}, videoTitle(video)), el("div", {class: "who"}, who)),
      el("div", {}, video.description
        ? [el("p", {class: "caption summary-line"}, video.description.en),
           el("p", {class: "small"}, `Description: ${DESCRIPTION_STATUS[video.description_status]}`,
              video.description.place ? `, place: ${video.description.place}` : "")]
        : el("p", {class: "small"}, "No description"))));
  const build = () => { if (!row.querySelector(".detail")) row.append(videoDetail(video)); };
  row.addEventListener("toggle", () => {
    if (row.open) { app.open.add(video.id); build(); } else app.open.delete(video.id);
  });
  if (app.open.has(video.id)) { build(); row.open = true; }
  return row;
}

function renderVideoList() {
  if (!app.library) { fill($("video-list")); $("video-count").textContent = ""; return; }
  const all = visibleVideos();
  const list = all.slice(0, app.shown);
  $("video-count").textContent = all.length === app.library.videos.length
    ? plural(all.length, "video") : `${all.length} of ${plural(app.library.videos.length, "video")}`;
  fill($("video-list"), 
    list.length ? list.map(videoRow) : el("p", {class: "sub"}, app.library.videos.length ? "No video matches these filters."
      : "No videos in the latest report yet."),
    all.length > list.length ? el("button", {type: "button", class: "more", onclick: () => { app.shown += PAGE_SIZE; renderVideoList(); }},
                                  `Show ${Math.min(PAGE_SIZE, all.length - list.length)} more`) : null);
}

function buildVideosView() {
  const results = [["all", "All"], ...RESULTS.map(([key, label]) => [key, label])];
  const buttons = results.map(([key, label]) => el("button", {type: "button", "data-result": key, "aria-pressed": String(key === "all"),
    onclick: () => { app.filter.result = key; app.shown = PAGE_SIZE; updateVideoControls(); renderVideoList(); }}));
  const person = el("select", {id: "person-filter", "aria-label": "Person",
    onchange: (e) => { app.filter.person = e.target.value; app.shown = PAGE_SIZE; renderVideoList(); }});
  const search = el("input", {type: "search", placeholder: "Search names, people, topics, objects", "aria-label": "Search videos",
    oninput: (e) => { app.filter.text = e.target.value; app.shown = PAGE_SIZE; renderVideoList(); }});
  fill($("view-videos"), 
    el("div", {class: "filters"}, search, el("div", {class: "seg", id: "result-filter", role: "group", "aria-label": "Result"}, buttons), person),
    el("div", {id: "live-note"}), el("p", {class: "count", id: "video-count"}), el("div", {id: "video-list"}));
}

function updateVideoControls() {
  fill($("live-note"), liveNote());
  const lib = app.library, counts = {all: lib.videos.length, auto: lib.totals.auto, review: lib.totals.review,
                                      none: lib.totals.none, error: lib.totals.errors};
  for (const button of $("result-filter").children) {
    const key = button.dataset.result;
    const label = key === "all" ? "All" : RESULT_LABEL[key];
    button.textContent = `${label} ${counts[key]}`;
    button.setAttribute("aria-pressed", String(app.filter.result === key));
  }
  const select = $("person-filter");
  fill(select, el("option", {value: "all"}, "Everyone"), lib.people.map((p) => el("option", {value: String(p.id)}, p.name)));
  select.value = lib.people.some((p) => String(p.id) === app.filter.person) ? app.filter.person : "all";
  app.filter.person = select.value;
}

// -- runs, log, health ---------------------------------------------------------------------------

function renderRuns() {
  const rows = app.history || [];
  const widest = Math.max(1, ...rows.map((r) => r.videos));
  fill($("view-runs"), 
    el("h2", {}, "Runs"),
    el("p", {class: "sub"}, "Every run saves a report, dry or applied. The newest ones are first."),
    rows.length ? el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Finished"), el("th", {class: "num"}, "Videos"), el("th", {}, "Result"),
        RESULTS.map(([, label]) => el("th", {class: "num"}, label)))),
      el("tbody", {}, rows.map((r) => el("tr", {},
        el("td", {}, when(r.time)), el("td", {class: "num"}, r.videos),
        el("td", {}, el("div", {style: `width:${Math.max(8, (r.videos / widest) * 100)}%`},
          segments([["Confident", r.auto, RESULT_COLORS.auto], ["Suggestion", r.review, RESULT_COLORS.review],
                    ["No match", r.none, RESULT_COLORS.none], ["Failed", r.errors, RESULT_COLORS.error]], "mini"))),
        [r.auto, r.review, r.none, r.errors].map((n) => el("td", {class: "num"}, n)))))) :
      el("p", {class: "sub", style: "margin-top:12px"}, "No reports yet."));
}

function buildLogView() {
  fill($("view-log"), 
    el("div", {class: "log-head"}, el("h2", {}, "Log"),
      el("label", {}, el("input", {type: "checkbox", id: "log-follow", checked: true}), " Follow the newest line")),
    el("p", {class: "sub"}, "The last lines of the run log. A headless or scheduled run writes to the same log."),
    el("div", {class: "log", id: "log", role: "log", tabindex: "0", "aria-label": "Run log"}));
}

function renderLog() {
  const box = $("log"), follow = $("log-follow").checked, scroll = box.scrollTop;
  const lines = app.status.log;
  fill(box, lines.length ? lines.map((line) => el("div", {},
    el("span", {class: "t"}, line.time.slice(11)),
    el("span", {class: `lvl ${line.level}`}, line.level === "ERROR" ? "✕" : line.level === "WARNING" ? "!" : ""),
    el("span", {class: line.level}, line.text))) : el("p", {class: "muted"}, "The log is empty."));
  box.scrollTop = follow ? box.scrollHeight : scroll;
}

const CHECK_MARK = {ok: "✓", warn: "!", fail: "✕"};

function renderHealth(result) {
  fill($("health-list"), result.checks
    ? result.checks.map((c) => el("li", {}, el("span", {class: c.status, title: c.status}, CHECK_MARK[c.status]), el("span", {}, c.name),
                                  el("span", {class: "muted"}, c.detail)))
    : el("li", {class: "muted"}, "Not checked yet."));
  $("health-time").textContent = result.at ? `Checked ${when(result.at)}.` : "";
}

function buildHealthView() {
  const button = el("button", {type: "button", id: "health-run", onclick: async () => {
    button.disabled = true;
    button.textContent = "Checking...";
    try { renderHealth(await api("/api/doctor", {method: "POST"})); } catch (error) { toast(error.message); }
    button.disabled = false;
    button.textContent = "Run the health check";
  }}, "Run the health check");
  fill($("view-health"), 
    el("h2", {}, "Health"),
    el("p", {class: "sub"}, "Checks what an unattended run needs: the data folder, ffmpeg, your NAS login, the models and the scheduled task. "
       + "It logs in to your NAS and can take a few seconds."),
    el("div", {class: "health-head", style: "margin-top:12px"}, button, el("span", {class: "muted", id: "health-time"})),
    el("ul", {class: "checks", id: "health-list"}));
}

// -- tabs ----------------------------------------------------------------------------------------

const TABS = ["overview", "videos", "runs", "log", "health"];

function selectTab(name, focus = false) {
  app.tab = name;
  for (const tab of TABS) {
    $(`tab-${tab}`).setAttribute("aria-selected", String(tab === name));
    $(`tab-${tab}`).tabIndex = tab === name ? 0 : -1;
    $(`view-${tab}`).hidden = tab !== name;
  }
  if (focus) $(`tab-${name}`).focus();
  history.replaceState(null, "", `#${name}`);
  if (name === "log" && app.status) renderLog();
}

function wireTabs() {
  TABS.forEach((name, index) => {
    $(`tab-${name}`).addEventListener("click", () => selectTab(name));
    $(`tab-${name}`).addEventListener("keydown", (event) => {
      const step = {ArrowRight: 1, ArrowLeft: -1}[event.key];
      if (step) { event.preventDefault(); selectTab(TABS[(index + step + TABS.length) % TABS.length], true); }
    });
  });
}

// -- the run dialog ------------------------------------------------------------------------------

function runOptions() {
  const apply = document.querySelector('input[name="mode"]:checked').value === "apply";
  const options = {apply, describe: $("run-describe").checked};
  if (apply) options.max_writes = $("run-all").checked ? "all" : $("run-cap").valueAsNumber;  // NaN while blank: the field is `required`, so the browser stops the submit
  if ($("run-preset").value) options.preset = $("run-preset").value;
  if (!apply) {  // a slice is for a dry run: an apply run always covers the whole library
    const limit = $("run-limit").value.trim();
    if (limit) options.limit = Number(limit);
    if ($("run-new").checked) options.new_only = true;
    if ($("run-album").value) options.album = Number($("run-album").value);
  }
  return options;
}

function runCommand(o) {  // only what differs from the saved settings: those are what a terminal would need to be told
  const saved = app.settings || {};
  return ["videotag run", o.apply && `--apply --max-writes ${Number.isNaN(o.max_writes) ? "<number>" : o.max_writes}`, o.preset && o.preset !== saved.preset && `--preset ${o.preset}`,
          o.describe !== saved.describe && (o.describe ? "--describe" : "--no-describe"), o.limit && `--limit ${o.limit}`,
          o.new_only && "--new-only", o.album && `--album ${o.album}`].filter(Boolean).join(" ");
}

function refreshRunDialog() {
  const o = runOptions();
  $("run-cap-row").hidden = $("run-all-row").hidden = $("run-warning").hidden = !o.apply;
  $("run-cap").disabled = !o.apply || $("run-all").checked;  // a field that is hidden or unused must not be checked: it would block the submit silently
  $("run-slice").hidden = o.apply;
  $("run-start").textContent = o.apply ? "Apply changes" : "Start dry run";
  $("run-command").textContent = runCommand(o);
  const preset = app.settings && app.settings.presets.find((p) => p.name === o.preset);
  $("run-preset-note").textContent = preset ? `${ruleText(preset)}. Videos that already have a saved result keep it.` : "";
}

function fillPresetChoices(select, chosen) {
  fill(select, app.settings.presets.map((p) => el("option", {value: p.name}, `${PRESET_NAMES[p.name]}: ${ruleText(p)}`)));
  select.value = chosen;
}

async function loadAlbums() {
  const button = $("albums-load"), failure = $("albums-error");
  button.disabled = true;
  button.textContent = "Loading...";
  failure.hidden = true;
  try {
    const albums = await api("/api/albums", {method: "POST"});
    fill($("run-album"), el("option", {value: ""}, "The whole library"),
         albums.map((album) => el("option", {value: String(album.id)}, album.name || `Album ${album.id}`)));
    if (!albums.length) { failure.textContent = "You have no albums of your own in Photos."; failure.hidden = false; }
  } catch (error) {
    failure.textContent = error.message;
    failure.hidden = false;
  }
  button.disabled = false;
  button.textContent = "Load my albums";
}

async function openRunDialog() {
  $("run-error")?.remove();
  try {
    if (!app.settings) app.settings = await api("/api/settings");
  } catch (error) {
    toast(`Could not load the settings: ${error.message}`);
    return;
  }
  fillPresetChoices($("run-preset"), app.settings.preset);
  $("run-describe").checked = app.settings.describe;
  $("run-limit").value = "";
  $("run-new").checked = false;
  $("run-all").checked = false;  // writing everything is chosen on purpose each time, never left over from the last run
  fill($("run-album"), el("option", {value: ""}, "The whole library"));
  $("albums-error").hidden = true;
  $("run-dialog").showModal();
  refreshRunDialog();
}

function wireRunDialog() {
  const dialog = $("run-dialog");
  $("run-open").addEventListener("click", openRunDialog);
  $("albums-load").addEventListener("click", loadAlbums);
  $("run-cancel").addEventListener("click", () => dialog.close());
  $("run-form").addEventListener("input", refreshRunDialog);
  $("run-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const options = runOptions();
    try {
      await api("/api/run", {method: "POST", body: JSON.stringify(options)});
    } catch (error) {
      $("run-error")?.remove();
      $("run-form").insertBefore(el("p", {class: "warning", id: "run-error", role: "alert"}, error.message), document.querySelector(".dialog-actions"));
      return;
    }
    dialog.close();
    toast(options.apply ? "Apply run started. Progress shows at the top." : "Dry run started. Progress shows at the top.");
    poll();
  });
}

// -- undo ----------------------------------------------------------------------------------------

function wireUndoDialog() {
  const dialog = $("undo-dialog");
  $("undo-open").addEventListener("click", () => {
    $("undo-error").hidden = true;
    dialog.showModal();
  });
  $("undo-cancel").addEventListener("click", () => dialog.close());
  $("undo-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("undo-start").disabled = true;  // one click is one undo
    try {
      await api("/api/undo", {method: "POST"});
    } catch (error) {
      $("undo-error").textContent = error.message;
      $("undo-error").hidden = false;
      return;
    } finally {
      $("undo-start").disabled = false;
    }
    dialog.close();
    toast("Undo started. Progress shows at the top.");
    poll();
  });
}

// -- the settings --------------------------------------------------------------------------------

function chosenPreset() { return document.querySelector('input[name="settings-preset"]:checked').value; }

function renderReanalyse() {
  const s = app.settings, name = chosenPreset(), touched = s.reanalyse.touched[name], pace = s.reanalyse.pace_seconds;
  const start = el("button", {type: "button", id: "reanalyse-start", onclick: async () => {
    start.disabled = true;
    try {
      await api("/api/run", {method: "POST", body: JSON.stringify({reanalyse: true, preset: name, describe: $("settings-describe").checked})});
    } catch (error) {
      start.disabled = false;
      $("settings-error").textContent = error.message;
      $("settings-error").hidden = false;
      return;
    }
    $("settings-dialog").close();
    toast("Re-analysis started as a dry run. Progress shows at the top.");
    poll();
  }}, `Re-analyse ${plural(touched, "video")} (dry run)`);
  fill($("settings-reanalyse"),
    el("h3", {}, "Results made under another preset"),
    el("p", {class: "sub"}, "A preset only applies to videos without a saved result. Results you already have are kept, whatever preset you choose."),
    touched === 0
      ? el("p", {style: "margin-top:8px"}, s.reanalyse.of ? `All ${plural(s.reanalyse.of, "saved result")} were made under this preset.` : "There are no saved results yet.")
      : [el("p", {style: "margin-top:8px"}, `${touched} of ${plural(s.reanalyse.of, "saved result")} ${touched === 1 ? "was" : "were"} made under another preset. `
            + `Redoing ${touched === 1 ? "it reads that video" : "them reads those videos"} again and writes nothing to Photos`
            + (pace ? `, and takes up to about ${duration(touched * pace)} (the pace of your last run, which also described every video).` : ".")),
         start]);
}

function openSettingsDialog() {
  const s = app.settings;
  $("settings-problem").hidden = !s.problem;
  $("settings-problem").textContent = s.problem ? `${s.problem}. Saving replaces that file.` : "";
  $("settings-error").hidden = true;
  $("settings-describe").checked = s.describe;
  fill($("settings-presets"), s.presets.map((p) => el("label", {class: "choice"},
    el("input", {type: "radio", name: "settings-preset", value: p.name, checked: p.name === s.preset}),
    el("span", {}, el("strong", {}, `${PRESET_NAMES[p.name]}: ${ruleText(p)}`),
       el("span", {class: "hint"}, savingText(p.name, s.estimate))))));
  renderReanalyse();
  $("settings-dialog").showModal();
}

function wireSettingsDialog() {
  const dialog = $("settings-dialog");
  $("settings-open").addEventListener("click", async () => {
    try {
      app.settings = await api("/api/settings");
    } catch (error) {
      toast(error.status === 404 ? "This dashboard is older than the page and has no settings. Close the videotag gui window and start it again."
                                 : `Could not load the settings: ${error.message}`);
      return;
    }
    openSettingsDialog();
  });
  $("settings-cancel").addEventListener("click", () => dialog.close());
  $("settings-presets").addEventListener("change", renderReanalyse);
  $("settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const save = $("settings-save");
    save.disabled = true;
    try {
      app.settings = await api("/api/settings", {method: "POST", body: JSON.stringify({preset: chosenPreset(), describe: $("settings-describe").checked})});
      dialog.close();
      toast("Settings saved. Every run uses them from now on.");
    } catch (error) {
      $("settings-error").textContent = error.message;
      $("settings-error").hidden = false;
    }
    save.disabled = false;
  });
}

// -- the account ---------------------------------------------------------------------------------

function renderAccountButton() {
  const account = app.account, button = $("account-open");
  const attention = account && (account.mismatch || (!account.current && account.problem));
  button.textContent = !account ? "Account" : attention ? "Account needs attention"
    : account.current ? account.current.label : "No account";
  button.title = account && account.current ? `Synology account: ${account.current.label}` : "Synology account";
  button.classList.toggle("attention", Boolean(attention));
}

async function loadAccount() {
  let problem = null;
  const generation = app.accountGeneration;
  try {
    const account = await api("/api/account");
    if (generation !== app.accountGeneration) return null;
    const identity = (value) => JSON.stringify([value.current, value.owner, value.mismatch]);
    const changed = app.account && identity(app.account) !== identity(account);
    app.account = account;
    if (changed) { resetAccountData(); poll(); }
    if (app.library) { renderVideoList(); renderOverview(); }  // the links to Photos need the account, which may arrive after the library
  } catch (error) {
    problem = error.status === 404
      ? "This dashboard is older than the page and has no account support. Close the videotag gui window and start it again."
      : `Could not load the account: ${error.message}`;
  }
  renderAccountButton();
  return problem;
}

function accountNote(account, running) {
  if (account.locked) {
    return "The login comes from the VIDEOTAG_* environment variables, which take priority over a stored one. "
         + "Remove them to switch accounts here.";
  }
  if (running) return "A run is in progress. Switch accounts when it has finished.";
  if (account.mismatch) {
    return `The saved data belongs to ${account.owner.label}, but the stored login is for ${account.current.label}. `
         + "Runs refuse to start until the two match. Enter the login of one of them below.";
  }
  return null;
}

function normalisedAddress(value) { return value.trim().replace(/\/+$/, ""); }

function accountEffect() {
  const account = app.account, url = normalisedAddress($("account-url").value), user = $("account-user").value.trim();
  if (!account || !url || !user) return "";
  const holder = account.owner || account.current;  // whose saved data is on disk now: that decides what moves
  if (holder && url === holder.url && user === holder.username) {
    return "Same account as the saved data: only the password is updated. The saved data stays where it is.";
  }
  const returning = account.parked.some((p) => p.url === url && p.username === user);
  return [
    returning ? "The saved data set aside for this account comes back." : "This account starts with no saved data.",
    holder ? `Any saved data of ${holder.label} (results, caches, reports and log) is set aside. Tags videotag wrote there stay in Photos. `
      + "To remove them, use Undo (or videotag undo) while that account is active, before switching." : "",
  ].join(" ").trim();
}

function refreshAccountEffect() { $("account-effect").textContent = accountEffect(); }

function openAccountDialog() {
  const account = app.account, running = app.status && app.status.status.state === "running";
  $("account-current").textContent = account.current
    ? `Using ${account.current.label} (${account.source}).` : (account.problem || "No login is stored yet.");
  const note = accountNote(account, running);
  $("account-note").hidden = !note;
  $("account-note").textContent = note || "";
  $("account-fields").disabled = $("account-save").disabled = Boolean(account.locked || running);
  $("account-url").value = account.current ? account.current.url : "";
  $("account-user").value = account.current ? account.current.username : "";
  $("account-password").value = "";
  $("account-error").hidden = true;
  fill($("account-parked"), account.parked.length ? [
    el("p", {}, "Saved data set aside for these accounts comes back when you switch to them:"),
    account.parked.map((p) => el("button", {type: "button", onclick: () => {
      $("account-url").value = p.url;
      $("account-user").value = p.username;
      $("account-password").value = "";
      $("account-password").focus();
      refreshAccountEffect();
    }}, p.label))] : null);
  $("account-parked").hidden = !account.parked.length;
  refreshAccountEffect();
  $("account-dialog").showModal();
}

function wireAccountDialog() {
  const dialog = $("account-dialog");
  $("account-open").addEventListener("click", async () => {
    const problem = await loadAccount();
    if (problem) toast(problem);  // a click must never do nothing
    else openAccountDialog();
  });
  $("account-cancel").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => { $("account-password").value = ""; });  // the password never outlives the dialog
  $("account-form").addEventListener("input", refreshAccountEffect);
  $("account-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = {url: $("account-url").value, username: $("account-user").value, password: $("account-password").value};
    const save = $("account-save");
    save.disabled = true;
    save.textContent = "Checking the login...";
    $("account-error").hidden = true;
    try {
      app.account = await api("/api/account", {method: "POST", body: JSON.stringify(body)});
      resetAccountData();
      dialog.close();
      renderAccountButton();
      toast(`Now using ${app.account.current.label}.`);
      renderHealth({checks: null, at: null});  // that result described the old account
      poll();
    } catch (error) {
      $("account-error").textContent = error.message;
      $("account-error").hidden = false;
    }
    $("account-password").value = "";
    save.disabled = false;
    save.textContent = "Check and use this account";
  });
}

// -- loading -------------------------------------------------------------------------------------

function clearSpot() {
  app.spot = null;
  renderSpot();
}

function resetAccountData() {
  app.accountGeneration += 1;
  app.dataGeneration += 1;
  clearSpot();
  app.open.clear();
  app.library = app.history = app.accuracy = app.status = app.version = null;
  renderOverview();
  renderVideoList();
  renderRuns();
}

async function loadData() {
  const accountGeneration = app.accountGeneration, dataGeneration = ++app.dataGeneration;
  clearSpot();
  const data = await Promise.all([api("/api/library"), api("/api/history"), api("/api/accuracy").catch(() => null)]);
  if (accountGeneration !== app.accountGeneration || dataGeneration !== app.dataGeneration) return;
  [app.library, app.history, app.accuracy] = data;
  renderOverview();
  updateVideoControls();
  renderVideoList();
  renderRuns();
  renderRun();  // the run strip may name the latest report
}

let pollTimer;
async function poll() {
  clearTimeout(pollTimer);
  const generation = app.accountGeneration;
  try {
    const status = await api("/api/status");
    if (generation !== app.accountGeneration) return;
    app.status = status;
    $("offline").hidden = true;
    $("stale").hidden = app.status.api === API_VERSION;
    renderRun();
    if (app.tab === "log") renderLog();
    if (app.status.version !== app.version) {
      app.version = app.status.version;
      await loadData();
    }
  } catch (error) {
    if (generation !== app.accountGeneration) return;
    $("offline").hidden = false;
  }
  if (generation !== app.accountGeneration) return;
  const running = app.status && app.status.status.state === "running";
  pollTimer = setTimeout(poll, running ? 1500 : 5000);
}

async function start() {
  wireTabs();
  wireRunDialog();
  wireUndoDialog();
  wireSettingsDialog();
  wireAccountDialog();
  buildVideosView();
  buildLogView();
  buildHealthView();
  selectTab(TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "overview");
  api("/api/doctor").then(renderHealth, () => {});
  api("/api/settings").then((settings) => { app.settings = settings; }, () => {});
  loadAccount();
  await poll();
}

start();
