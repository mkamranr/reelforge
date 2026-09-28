/* One reel: the stage rail, the active pane, and live activity.
 *
 * The job id is in the URL, which is the point of a separate page -- the v1 UI
 * had no URL for anything, so a reload always landed on the first job. The
 * selected stage lives in the hash, so a link can point at a specific pane.
 */
(function (RF) {
  "use strict";

  var el, fmt;
  var state = { id: null, job: null, stage: null, stream: null, lines: [], group: null };

  function q(name) {
    return new URLSearchParams(location.search).get(name);
  }

  function artifactUrl(path) {
    return "/api/jobs/" + encodeURIComponent(state.id) + "/artifacts/" + path;
  }

  /* ------------------------------------------------------------------ head */
  function renderHead() {
    var job = state.job;
    var host = document.querySelector("[data-job-head]");
    if (!host) return;
    document.title = job.slug + " · ReelForge";

    var resolved = job.resolved || {};
    RF.dom.mount(host, [
      el("div", { class: "cluster", "data-gap": "3" },
        el("h1", { class: "section__title" }, job.slug),
        job.failed_stage ? el("span", { class: "chip chip--failed" },
                              "failed at " + RF.stages.label(job.failed_stage)) : null,
        !job.next_stage ? el("span", { class: "chip chip--done" }, "finished") : null,
        el("span", { class: "grow" }),
        el("span", { "data-queue-chip": "" }),
        el("a", { class: "btn btn--sm",
                  href: "/v2/images.html?id=" + encodeURIComponent(state.id) },
           RF.icon("image", { size: 15 }),
           "Screenshots" + (job.images && job.images.length
                            ? " (" + job.images.length + ")" : "")),
        el("a", { class: "btn btn--sm btn--ghost", href: "/v2/" }, "All reels")),
      el("div", { class: "jobcard__meta" },
        job.source && job.source.url ? el("a", { href: job.source.url, target: "_blank",
                                                 rel: "noreferrer" }, job.source.url) : null,
        " · " + job.template +
        " · captions " + (job.captions ? "on" : "off") +
        " · facts " + (job.fact_check || "strict") +
        (job.images && job.images.length ? " · " + job.images.length + " screenshot" +
                                           (job.images.length > 1 ? "s" : "") : "")),
      resolved.content ? el("div", { class: "jobcard__meta" },
        "script by " + (resolved.content.profile || "?") +
        (resolved.voice ? " · narration by " + resolved.voice.profile : "")) : null,
    ]);
  }

  /* ------------------------------------------------------------------ rail */
  function renderRail() {
    var host = document.querySelector("[data-rail]");
    if (!host) return;
    RF.dom.mount(host, el("nav", { class: "rail card card--flush", "aria-label": "Pipeline" },
      (state.job.stages || []).map(function (stage) {
        var meta = RF.stages.status(stage.status);
        var selected = stage.stage === state.stage;
        return el("button", {
          class: "rail__item" + (selected ? " rail__item--on" : ""),
          "aria-current": selected ? "true" : null,
          title: RF.stages.blurb(stage.stage),
          onclick: function () { select(stage.stage); },
        },
          el("span", { class: "rail__dot", dataset: { status: stage.status } }),
          el("span", { class: "grow" },
            el("span", { class: "rail__name" }, RF.stages.label(stage.stage)),
            el("span", { class: "rail__meta" },
               meta.label +
               (stage.duration_seconds ? " · " + fmt.duration(stage.duration_seconds) : "") +
               (stage.attempts > 1 ? " · " + stage.attempts + " attempts" : ""))));
      })));
    renderActions();
  }

  function renderActions() {
    var host = document.querySelector("[data-actions]");
    if (!host) return;
    var job = state.job;
    var current = (job.stages || []).filter(function (s) { return s.stage === state.stage; })[0];
    var running = (job.stages || []).some(function (s) { return s.status === "running"; });

    RF.dom.mount(host, [
      job.next_stage ? el("button", {
        class: "btn btn--primary", disabled: running,
        onclick: function () { act("/run", {}, "queued"); },
      }, RF.icon(running ? "clock" : "play", { size: 15 }),
         running ? "Running…" : "Run to next gate") : null,

      current && current.status === "review" ? el("button", {
        class: "btn",
        onclick: function () {
          act("/stages/" + state.stage + "/approve", {}, "approved");
        },
      }, RF.icon("check", { size: 15 }),
         "Approve " + RF.stages.label(state.stage)) : null,

      current && current.status !== "pending" ? el("button", {
        class: "btn btn--ghost", disabled: running,
        onclick: function () {
          act("/stages/" + state.stage + "/retry", {}, "retrying");
        },
      }, RF.icon("refresh", { size: 15 }),
         "Retry " + RF.stages.label(state.stage)) : null,
    ]);
  }

  function act(suffix, body, verb) {
    return RF.api.post("/api/jobs/" + encodeURIComponent(state.id) + suffix, body)
      .then(function () { RF.dom.toast(verb, { kind: "ok" }); return refresh(); })
      .catch(function (error) { RF.dom.toast(error.message, { kind: "error", timeout: 9000 }); });
  }

  /* ----------------------------------------------------------------- panes */
  var panes = {};

  function metaCard(stage) {
    var rows = Object.keys(stage.meta || {}).filter(function (key) {
      var value = stage.meta[key];
      return value === null || ["string", "number", "boolean"].indexOf(typeof value) !== -1;
    });
    if (!rows.length) return null;
    return el("div", { class: "card" },
      el("table", { class: "table table--dense" },
        el("tbody", null, rows.map(function (key) {
          return el("tr", null,
            el("th", null, key.replace(/_/g, " ")),
            el("td", { class: "mono" }, String(stage.meta[key])));
        }))));
  }

  panes.cover = function (stage) {
    return el("div", { class: "card" },
      el("img", { src: artifactUrl(stage.artifacts.cover), alt: "Cover art",
                  class: "shot", loading: "lazy" }));
  };

  /* Every generated picture with the prompt that made it. Stills show the
   * prepared bitmap the renderer will paste; a clip shows its first and
   * middle frame, since the frames are what the reel uses, not the mp4. */
  panes.visuals = function (stage) {
    var meta = stage.meta || {};
    if (meta.enabled === false) {
      return el("div", { class: "callout callout--info" },
        el("div", null,
          el("div", { class: "callout__title" }, "Generated visuals are off for this reel"),
          el("div", null, "Turn them on under Settings, or per reel in the job settings, " +
             "and the stage will draw stills and a clip from the script's directions.")));
    }
    return RF.api.get(artifactUrl(stage.artifacts.visuals || "visuals/visuals.json"))
      .then(function (manifest) {
        var assets = manifest.assets || [];
        if (!assets.length) {
          return el("div", { class: "callout callout--info" },
            el("div", null, "Nothing was generated: " + (meta.note || "the script had no scenes to illustrate.")));
        }
        return el("div", { class: "stack", "data-gap": "3" }, assets.map(function (asset) {
          var title = asset.kind === "music"
            ? "music bed"
            : asset.kind + " " + asset.index + " — scene " + asset.scene_index +
              (asset.scene_title ? ": " + asset.scene_title : "");
          var pictures;
          if (!asset.ok) {
            pictures = el("div", { class: "callout callout--error" },
              el("div", null, el("div", { class: "callout__title" }, "failed"),
                 el("div", null, asset.error || "no detail")));
          } else if (asset.kind === "still") {
            pictures = el("img", { src: artifactUrl(asset.file), alt: title,
                                   class: "shot", loading: "lazy" });
          } else if (asset.kind === "music") {
            pictures = el("audio", { src: artifactUrl(asset.source || asset.file),
                                     controls: "controls", class: "shot", preload: "none" });
          } else if (asset.source) {
            pictures = el("video", { src: artifactUrl(asset.source), controls: "controls",
                                     class: "shot", preload: "metadata", loop: "loop", muted: "muted",
                                     poster: artifactUrl(asset.file + "/00001.jpg") });
          } else {
            var mid = Math.max(1, Math.floor((asset.frames || 1) / 2));
            pictures = el("div", { class: "cluster", "data-gap": "2" },
              el("img", { src: artifactUrl(asset.file + "/00001.jpg"), alt: title + " first frame",
                          class: "shot shot--half", loading: "lazy" }),
              el("img", { src: artifactUrl(asset.file + "/" + String(mid).padStart(5, "0") + ".jpg"),
                          alt: title + " middle frame", class: "shot shot--half", loading: "lazy" }));
          }
          return el("div", { class: "card stack", "data-gap": "2" },
            el("div", { class: "card__head" },
              el("span", { class: "card__title grow" }, title),
              el("span", { class: "chip " + (asset.ok ? "chip--done" : "chip--failed") },
                 asset.ok ? (asset.kind === "clip"
                              ? asset.frames + " frames @ " + asset.fps + " fps"
                              : asset.kind === "music"
                                ? asset.seconds + " s"
                                : asset.width + "×" + asset.height)
                          : "failed")),
            pictures,
            el("div", { class: "field__hint mono" }, "seed " + asset.seed),
            el("div", { class: "field__hint" }, asset.prompt));
        }));
      })
      .catch(function () { return metaCard(stage); });
  };

  /* The reel, and the sizes it can be delivered at.
   *
   * 1080x1920 is what the pipeline renders and what `verify` checks, so it is
   * always the master. 2K and 4K are Lanczos upscales of that same file,
   * written beside it -- they add no detail, which is why they are a button
   * rather than a default. The encode is slow enough to be worth a busy state:
   * measured at roughly 30 s for 2K and 70 s for 4K on a 40-second reel.
   */
  panes.render = function (stage) {
    var url = "/api/jobs/" + encodeURIComponent(state.id) + "/upscale";
    return RF.api.get(url + "s").then(function (info) {
      var sizes = info.sizes || [];
      var select = el("select", { class: "select" }, sizes.map(function (size) {
        var label = size.resolution === "1080p"
          ? "1080p — " + size.width + "x" + size.height + " (native)"
          : size.resolution.toUpperCase() + " — " + size.width + "x" + size.height
            + (size.exists ? " ✓" : "");
        return el("option", { value: size.resolution }, label);
      }));

      var button = el("button", { class: "btn btn--sm", type: "button" }, "Make");
      button.addEventListener("click", function () {
        var choice = select.value;
        if (choice === "1080p") {
          RF.dom.toast("1080p is the rendered master — it already exists", { kind: "ok" });
          return;
        }
        button.dataset.busy = "1";
        RF.dom.toast("upscaling to " + choice.toUpperCase() + " — this takes a minute",
                     { kind: "ok", timeout: 8000 });
        RF.api.post(url, { resolution: choice })
          .then(function (made) {
            RF.dom.toast(choice.toUpperCase() + " written: " + fmt.bytes(made.bytes),
                         { kind: "ok", timeout: 9000 });
            renderPane();
          })
          .catch(function (error) {
            RF.dom.toast(error.message, { kind: "error", timeout: 12000 });
          })
          .then(function () { delete button.dataset.busy; });
      });

      var made = sizes.filter(function (s) { return s.exists; });
      return el("div", { class: "stack", "data-gap": "3" },
        el("div", { class: "card" },
          el("video", { src: artifactUrl(stage.artifacts.video), controls: "controls",
                        class: "shot", preload: "metadata" })),
        el("div", { class: "card" },
          el("div", { class: "card__title" }, "Deliverable size"),
          el("div", { class: "field__hint" },
             "The reel renders at 1080x1920, which is what the platform checks "
             + "passed. Larger sizes are upscales of that file — no extra detail, "
             + "but some platforms give a bigger upload a better bitrate."),
          el("div", { class: "cluster", "data-gap": "2" }, select, button),
          el("div", { class: "cluster", "data-gap": "2" }, made.map(function (size) {
            var file = String(size.path).split(/[\\/]/).pop();
            return el("a", { class: "btn btn--sm", href: artifactUrl(size.path),
                             download: file },
                      RF.icon("download", { size: 14 }),
                      size.resolution.toUpperCase() + " · " + fmt.bytes(size.bytes));
          }))));
    });
  };

  panes.verify = function (stage) {
    return RF.api.get(artifactUrl(stage.artifacts.report || "verify.json")).then(function (report) {
      return el("div", { class: "card card--flush" },
        el("div", { class: "card__head" },
          el("span", { class: "card__title grow" }, "Platform checks"),
          el("span", { class: "chip " + (report.ok ? "chip--done" : "chip--failed") },
             report.ok ? "all pass" : "failed")),
        el("table", { class: "table" },
          el("thead", null, el("tr", null,
            el("th", null, "check"), el("th", null, "expected"), el("th", null, "actual"))),
          el("tbody", null, (report.checks || []).map(function (check) {
            return el("tr", null,
              el("th", null,
                el("span", { class: "chip " + (check.ok ? "chip--done" : "chip--failed") },
                   check.ok ? "ok" : "fail"), " ", check.name),
              el("td", { class: "muted" }, String(check.expected === undefined ? "" : check.expected)),
              el("td", { class: "mono" }, String(check.actual === undefined ? "" : check.actual)));
          }))));
    });
  };

  panes.package = function (stage) {
    return el("div", { class: "stack", "data-gap": "3" },
      el("div", { class: "card cluster" },
        Object.keys(stage.artifacts || {}).map(function (key) {
          var file = String(stage.artifacts[key]).split(/[\\/]/).pop();
          return el("a", { class: "btn btn--sm", href: artifactUrl(stage.artifacts[key]),
                           download: file },
                    RF.icon("download", { size: 14 }), key);
        })));
  };

  /* ------------------------------------------------------------ audio ---
   * Two ways to get narration: synthesize it, or bring your own. The upload
   * path matters more than it looks -- a recording you made yourself is the
   * only way to get a voice the TTS providers do not have, and it is what the
   * six shipped reels used.
   *
   * Reads the probe-free /api/config/profiles, not /api/config/providers:
   * the latter does live network probes and once made a form take 16 seconds.
   * Reachability arrives afterwards and only annotates.
   */
  panes.audio = function (stage) {
    return RF.api.get("/api/config/profiles").then(function (profiles) {
      var voices = (profiles.tts || {}).providers || [];
      var chosen = (state.job.providers || {}).tts_provider || (profiles.tts || {}).selected;
      var have = stage.artifacts && stage.artifacts.audio;

      var select = el("select", { class: "select" },
        voices.map(function (p) {
          return el("option", { value: p.provider, selected: p.provider === chosen },
                    p.provider + (p.adapter ? " — " + p.adapter : ""));
        }));
      var file = el("input", { type: "file", accept: "audio/*", class: "input" });
      var status = el("div", { class: "field__hint" });

      function go() {
        var chain = Promise.resolve();
        if (file.files && file.files[0]) {
          var form = new FormData();
          form.append("file", file.files[0]);
          status.textContent = "uploading " + file.files[0].name + "…";
          chain = RF.api.upload("/api/jobs/" + encodeURIComponent(state.id) + "/audio", form);
        }
        return chain.then(function () {
          status.textContent = "queued";
          return act("/run", { stage: "audio" }, "audio queued");
        }).catch(function (error) {
          status.textContent = "";
          RF.dom.toast(error.message, { kind: "error", timeout: 9000 });
        });
      }

      return el("div", { class: "stack", "data-gap": "3" },
        have ? el("div", { class: "card stack", "data-gap": "3" },
          el("div", { class: "card__title" }, "Narration"),
          el("audio", { controls: "controls", class: "shot",
                        src: artifactUrl(stage.artifacts.audio) }),
          (stage.meta || {}).duration_seconds
            ? el("div", { class: "field__hint" },
                 fmt.duration(stage.meta.duration_seconds) + " of narration" +
                 (stage.meta.provider ? " · " + stage.meta.provider : "") +
                 // The reel is the narration plus a beat stretched to reach the
                 // platform floor. The audio stage already computed it, so read
                 // it rather than reimplementing that arithmetic here -- the
                 // constants have moved once already.
                 (stage.meta.reel_seconds
                    ? " · makes a " + Math.round(stage.meta.reel_seconds) + "s reel"
                    : ""))
            : null) : null,

        el("div", { class: "card stack", "data-gap": "4" },
          el("div", { class: "card__title" }, have ? "Replace the narration" : "Add narration"),
          el("label", { class: "field" },
            el("span", { class: "field__label" }, "Synthesize with"),
            select,
            el("span", { class: "field__hint" },
               "Each phrase is synthesized separately and joined with the pauses the "
               + "script asks for, so the aligner finds exactly as many segments as "
               + "there are phrases.")),
          el("label", { class: "field" },
            el("span", { class: "field__label" }, "Or upload a recording"),
            file,
            el("span", { class: "field__hint" },
               "Your own voice, or a take from anywhere else. The phrase list is "
               + "reconciled against it in the next stage.")),
          el("div", { class: "cluster" },
            el("button", { class: "btn btn--primary", onclick: go }, "Use this"),
            status)));
    });
  };

  /* ------------------------------------------------------------- align ---
   * The reconciliation view. `align.py build` refuses to run unless the phrase
   * count matches the number of detected segments, and the honest resolution
   * when it does not is a human splitting or merging lines -- which is exactly
   * what the current process does in a text editor.
   */
  panes.align = function (stage) {
    return RF.api.get("/api/jobs/" + encodeURIComponent(state.id) + "/alignment")
      .then(function (data) { return alignView(data); })
      .catch(function (error) {
        return el("div", { class: "callout callout--error" },
          el("div", null,
            el("div", { class: "callout__title" }, "The narration could not be probed"),
            el("div", null, error.message)));
      });
  };

  function alignView(data) {
    var editor = null;

    var rows = el("table", { class: "table table--dense" },
      el("thead", null, el("tr", null,
        el("th", null, "#"), el("th", { class: "table__num" }, "start"),
        el("th", { class: "table__num" }, "end"), el("th", { class: "table__num" }, "for"),
        el("th", null, "phrase"))),
      el("tbody", null, data.rows.map(function (row) {
        var orphanSegment = row.start !== null && !row.phrase;
        var orphanPhrase = row.start === null && row.phrase;
        return el("tr", null,
          el("th", { class: "mono" }, String(row.index + 1)),
          el("td", { class: "table__num mono" }, row.start === null ? "—" : row.start.toFixed(2)),
          el("td", { class: "table__num mono" }, row.end === null ? "—" : row.end.toFixed(2)),
          el("td", { class: "table__num mono" }, row.duration === null ? "—" : row.duration + "s"),
          el("td", { class: orphanSegment || orphanPhrase ? "warn" : "" },
             row.phrase || el("span", { class: "muted" }, "nothing said here")));
      })));

    var header = data.matched
      ? el("div", { class: "callout callout--ok" },
          el("div", null,
            el("div", { class: "callout__title" }, "The narration and the script agree"),
            el("div", null, data.detected + " spoken phrases, " + data.phrases +
               " lines in the script — alignment will run.")))
      : el("div", { class: "callout callout--warn" },
          el("div", null,
            el("div", { class: "callout__title" },
               "The narration splits into " + data.detected + " phrases but the script has " +
               data.phrases),
            el("div", null,
               "Alignment refuses to guess. Edit the lines below so there is exactly " +
               "one per spoken phrase — split a line that covers two, or join two that " +
               "cover one. Every break should land on a real clause boundary.")));

    var actions = null;
    if (!data.matched) {
      editor = el("textarea", { class: "textarea", rows: "14" },
                  data.rows.map(function (r) { return r.phrase; })
                           .filter(Boolean).join("\n"));
      actions = el("div", { class: "card stack", "data-gap": "3" },
        el("div", { class: "card__title" },
           "Phrase list — one line per spoken phrase"),
        editor,
        el("div", { class: "cluster" },
          el("button", { class: "btn btn--primary", onclick: function () {
            var lines = editor.value.split("\n").map(function (l) { return l.trim(); })
                                    .filter(Boolean);
            RF.api.put("/api/jobs/" + encodeURIComponent(state.id) + "/phrases", lines)
              .then(function () {
                RF.dom.toast("phrase list saved", { kind: "ok" });
                return act("/run", { stage: "align" }, "align queued");
              })
              .catch(function (error) {
                RF.dom.toast(error.message, { kind: "error", timeout: 9000 });
              });
          } }, "Save and align"),
          el("span", { class: "field__hint", "data-count": "" },
             "Needs " + data.detected + " lines")));

      editor.addEventListener("input", function () {
        var n = editor.value.split("\n").filter(function (l) { return l.trim(); }).length;
        var hint = actions.querySelector("[data-count]");
        hint.textContent = n === data.detected
          ? n + " lines — matches"
          : n + " lines, needs " + data.detected;
        hint.className = "field__hint" + (n === data.detected ? " ok" : " warn");
      });
    }

    return el("div", { class: "stack", "data-gap": "3" },
      header,
      actions,
      el("div", { class: "card card--flush" },
        el("div", { class: "card__head" },
          el("span", { class: "card__title grow" }, "Detected phrases"),
          el("span", { class: "badge mono" },
             fmt.duration(data.duration) + " of narration"),
          el("span", { class: "badge mono" },
             "drop " + data.drop + " dB · gap " + data.min_sil + " ms")),
        rows));
  }

  panes.content = function (stage) {
    return RF.api.get("/api/jobs/" + encodeURIComponent(state.id) + "/content")
      .then(function (content) {
        return el("div", { class: "stack", "data-gap": "3" },
          factsCard(stage),
          narrationEditor(content),
          el("div", { class: "card card--flush" },
            el("div", { class: "card__head" }, el("span", { class: "card__title" }, "Fact sheet")),
            el("table", { class: "table table--dense" },
              el("tbody", null, (content.fact_sheet || []).map(function (row) {
                return el("tr", null, el("th", null, row.label),
                                      el("td", { class: "mono" }, row.value));
              })))));
      });
  };

  /* One phrase per line as `scene | text`, which is the shape the PUT expects
   * and the shape the aligner counts. Editing it re-runs from content, so the
   * button says so rather than leaving you to discover it. */
  function narrationEditor(content) {
    var box = el("textarea", { class: "textarea", rows: "14" },
      content.phrases.map(function (p) {
        return p.scene_index + " | " + p.text;
      }).join("\n"));

    var count = el("span", { class: "field__hint" });
    function recount() {
      var lines = box.value.split("\n").filter(function (l) { return l.trim(); });
      var words = lines.join(" ").split(/\s+/).filter(Boolean).length;
      count.textContent = lines.length + " phrases · " + words + " words";
    }
    box.addEventListener("input", recount);
    recount();

    return el("div", { class: "card card--flush" },
      el("div", { class: "card__head" },
        el("span", { class: "card__title grow" }, "Narration"),
        count),
      el("div", { class: "card__body stack", "data-gap": "3" },
        box,
        el("div", { class: "field__hint" },
           "One phrase per line, as scene | text. Each becomes one caption group " +
           "and one aligned segment."),
        el("div", { class: "cluster" },
          el("button", { class: "btn btn--primary", onclick: function (event) {
            saveContent(content, box, event.target);
          } }, RF.icon("check", { size: 15 }), "Save and re-run from here"),
          el("button", { class: "btn btn--ghost", onclick: function () {
            box.value = content.phrases.map(function (p) {
              return p.scene_index + " | " + p.text;
            }).join("\n");
            recount();
          } }, "Revert"))));
  }

  function saveContent(content, box, button) {
    var lines = box.value.split("\n").map(function (l) { return l.trim(); })
                         .filter(Boolean);
    var phrases = lines.map(function (line, index) {
      var bar = line.indexOf("|");
      var scene = bar === -1 ? (content.phrases[index] || {}).scene_index || 1
                             : parseInt(line.slice(0, bar), 10);
      var text = bar === -1 ? line : line.slice(bar + 1).trim();
      return { scene_index: isNaN(scene) ? 1 : scene, text: text };
    });

    var payload = Object.assign({}, content, { phrases: phrases });
    button.dataset.busy = "1";
    RF.api.put("/api/jobs/" + encodeURIComponent(state.id) + "/content", payload)
      .then(function (result) {
        var cleared = (result && result.cleared) || [];
        RF.dom.toast(cleared.length
          ? "saved — cleared " + cleared.join(", ")
          : "saved", { kind: "ok", timeout: 7000 });
        return refresh();
      })
      .catch(function (error) {
        RF.dom.toast(error.message, { kind: "error", timeout: 12000 });
      })
      .then(function () { delete button.dataset.busy; });
  }

  function factsCard(stage) {
    var facts = (stage.meta || {}).facts;
    if (!facts) return null;
    var bad = facts.unsourced || [];
    if (!bad.length) {
      return el("div", { class: "callout callout--ok" },
        el("div", null,
          el("div", { class: "callout__title" }, "Every figure traces"),
          el("div", null, facts.total + " numbers — " + facts.api + " from the API, " +
             facts.readme + " from the project's README.")));
    }
    return el("div", { class: "callout callout--warn" },
      el("div", null,
        el("div", { class: "callout__title" }, bad.length + " figures do not trace"),
        el("div", null, "Neither the API nor the project's README states these."),
        el("table", { class: "table table--dense" },
          el("tbody", null, bad.map(function (row) {
            return el("tr", null, el("th", { class: "mono" }, row.value),
                                  el("td", { class: "muted" }, row.context));
          })))));
  }

  panes.storyboard = function (stage) {
    return RF.api.text("/api/jobs/" + encodeURIComponent(state.id) + "/storyboard")
      .then(function (source) {
        var box = el("textarea", { class: "textarea", rows: "24" }, source);
        var problems = el("div", { class: "stack", "data-gap": "2" });

        return el("div", { class: "stack", "data-gap": "3" },
          attemptsCard(stage),
          problems,
          el("div", { class: "card card--flush" },
            el("div", { class: "card__head" },
              el("span", { class: "card__title grow" }, "Storyboard"),
              el("span", { class: "field__hint" }, "Python, run by the renderer")),
            el("div", { class: "card__body stack", "data-gap": "3" },
              box,
              el("div", { class: "cluster" },
                el("button", { class: "btn btn--primary", onclick: function (event) {
                  saveStoryboard(box, problems, event.target);
                } }, RF.icon("check", { size: 15 }), "Save and check"),
                el("button", { class: "btn btn--ghost", onclick: function () {
                  box.value = source; RF.dom.clear(problems);
                } }, "Revert"),
                el("span", { class: "field__hint" },
                   "It is parsed, checked against the words actually spoken, and " +
                   "rendered as sample frames before it is accepted."))))); 
      });
  };

  function attemptsCard(stage) {
    var attempts = ((stage.meta || {}).attempts) || [];
    if (!attempts.length) return null;
    return el("div", { class: "card card--flush" },
      el("div", { class: "card__head" },
        el("span", { class: "card__title" }, "Generation attempts")),
      el("table", { class: "table table--dense" },
        el("tbody", null, attempts.map(function (attempt) {
          return el("tr", null,
            el("th", null, "attempt " + attempt.attempt),
            el("td", null,
               el("span", { class: "chip " + (attempt.ok ? "chip--done" : "chip--failed") },
                  attempt.ok ? "accepted" : (attempt.stage || "rejected")),
               (attempt.problems || []).map(function (problem) {
                 return el("div", { class: "field__hint" }, problem);
               })));
        }))));
  }

  /* PUT /storyboard runs the whole validation ladder and answers
   * 422 {"problems": [...]} -- which is the useful part, so it is rendered as a
   * list rather than collapsed into one toast. */
  function saveStoryboard(box, host, button) {
    button.dataset.busy = "1";
    RF.dom.clear(host);
    RF.api.request("/api/jobs/" + encodeURIComponent(state.id) + "/storyboard", {
      method: "PUT", body: box.value,
      headers: { "content-type": "text/plain" }, expect: "text",
    })
      .then(function () {
        RF.dom.toast("storyboard accepted", { kind: "ok" });
        return refresh();
      })
      .catch(function (error) {
        var problems = (error.detail && error.detail.problems) || [error.message];
        RF.dom.mount(host, RF.dom.callout("error",
          problems.length + " problem(s) — the storyboard was not saved",
          el("ul", { class: "stack", "data-gap": "1" },
             problems.map(function (problem) {
               return el("li", { class: "mono" }, problem);
             }))));
      })
      .then(function () { delete button.dataset.busy; });
  }

  function renderPane() {
    var host = document.querySelector("[data-pane]");
    if (!host) return;
    if (state.group) state.group.abort();
    state.group = RF.api.group();

    var stage = (state.job.stages || []).filter(function (s) {
      return s.stage === state.stage;
    })[0];
    if (!stage) return;

    renderArtifacts(stage);

    if (stage.status === "pending") {
      return RF.dom.mount(host, el("div", { class: "empty" },
        el("div", { class: "empty__art" }),
        el("div", { class: "empty__title" }, RF.stages.label(stage.stage) + " has not run"),
        el("div", { class: "empty__body" }, RF.stages.blurb(stage.stage) + "."),
        (stage.blocked_by || []).length
          ? el("div", { class: "muted" }, "Waiting on " +
              stage.blocked_by.map(RF.stages.label).join(", "))
          : null));
    }

    /* A stage that is still working has not written its artifact yet, so
     * fetching one answers 404 -- "no storyboard yet" is the literal message.
     * That is a normal state, not a failure, and rendering it as an error made
     * a working pipeline look broken. Show the work instead; SSE refreshes this
     * the moment the stage finishes. */
    if (stage.status === "running") {
      RF.dom.mount(host, el("div", { class: "stack", "data-gap": "3" },
        el("div", { class: "card cluster", "data-gap": "3" },
          el("span", { class: "spinner" }),
          el("div", { class: "grow" },
            el("div", { class: "card__title" },
               RF.stages.label(stage.stage) + " is running"),
            el("div", { class: "field__hint" },
               RF.stages.blurb(stage.stage) + ". Progress appears in the activity "
               + "panel as it happens." +
               (stage.started_at ? " Started " + fmt.relTime(stage.started_at) + "." : ""))),
          typicalHint(stage.stage)),
        // the attempts so far are the useful detail during a long codegen
        stage.stage === "storyboard" ? attemptsCard(stage) : null,
        metaCard(stage)));
      return;
    }

    if (stage.status === "failed") {
      RF.dom.mount(host, el("div", { class: "stack", "data-gap": "3" },
        el("div", { class: "callout callout--error" },
          el("div", null,
            el("div", { class: "callout__title" }, RF.stages.label(stage.stage) + " failed"),
            el("div", { class: "mono" }, stage.error || "no message recorded"))),
        metaCard(stage)));
      return;
    }

    var build = panes[stage.stage];
    if (!build) return RF.dom.mount(host, metaCard(stage) || el("div", { class: "empty" },
      el("div", { class: "empty__title" }, "Nothing to show for this stage")));

    RF.dom.mount(host, RF.dom.skeleton("card", 1));
    Promise.resolve(build(stage)).then(function (node) {
      RF.dom.mount(host, [node, metaCard(stage)].filter(Boolean));
    }).catch(function (error) {
      // 404 here means the artifact is not written yet -- a stage that was
      // running when this pane opened, or one whose output was invalidated.
      var pending = error.status === 404;
      RF.dom.mount(host, RF.dom.callout(
        pending ? "info" : "error",
        pending ? "Nothing written yet"
                : "Could not load this stage",
        pending ? RF.stages.label(stage.stage) + " has not produced its output yet. "
                  + "If it is running, this fills in when it finishes."
                : error.message));
    });
  }

  /* The measured medians from the jobs already on disk. Saying "this usually
   * takes about five minutes" is the difference between waiting and wondering
   * whether it has hung. */
  var TYPICAL = { ingest: 2, content: 207, cover: 4, audio: 15, align: 1,
                  storyboard: 309, render: 117, verify: 25, package: 1 };

  function typicalHint(stage) {
    var seconds = TYPICAL[stage];
    if (!seconds || seconds < 30) return null;
    return el("span", { class: "badge" }, "usually ~" + fmt.duration(seconds));
  }

  function renderArtifacts(stage) {
    var host = document.querySelector("[data-artifacts]");
    if (!host) return;
    var names = Object.keys(stage.artifacts || {});
    if (!names.length) return RF.dom.clear(host);
    RF.dom.mount(host, el("div", { class: "card card--flush" },
      el("div", { class: "card__head" }, el("span", { class: "card__title" }, "Files")),
      el("div", { class: "card__body stack", "data-gap": "2" },
        names.map(function (name) {
          return el("div", null,
            el("a", { href: artifactUrl(stage.artifacts[name]) }, name),
            el("div", { class: "jobcard__meta mono truncate" }, stage.artifacts[name]));
        }))));
  }

  /* ------------------------------------------------------------------ live */
  function note(line) {
    state.lines.push(line);
    if (state.lines.length > 400) state.lines.shift();
    var log = document.querySelector("[data-log]");
    if (!log) return;
    log.appendChild(el("div", { class: "log__line" }, line));
    log.scrollTop = log.scrollHeight;
  }

  function live(text, cls) {
    var pill = document.querySelector("[data-live]");
    if (!pill) return;
    pill.textContent = text;
    pill.className = "badge" + (cls ? " " + cls : "");
  }

  function connect() {
    if (state.stream) state.stream.close();
    state.stream = RF.api.events(state.id, {
      onOpen: function () { live("live", "chip chip--running"); },
      onHeartbeat: function () { live("live", "chip chip--running"); },
      onProgress: function (event) { note(event.message || JSON.stringify(event)); },
      onStage: function (event) {
        note(event.stage + ": " + event.status);
        refresh();
      },
      onPipeline: function (event) { note("pipeline " + event.status); refresh(); },
      onLost: function () { live("disconnected", "chip chip--failed"); },
    });
  }

  /* Where this reel sits in the line, if it is in it at all. Without this the
   * job page cannot explain why a reel that was "started" is not moving. */
  function queueChip() {
    var host = document.querySelector("[data-queue-chip]");
    if (!host) return;
    RF.api.get("/api/queue").then(function (payload) {
      var mine = RF.queue.index(payload).get(state.id);
      if (!mine || (mine.state !== "queued" && mine.state !== "running")) {
        return RF.dom.clear(host);
      }
      RF.dom.mount(host, el("span", {
        class: "chip " + (mine.state === "running" ? "chip--running" : "chip--queued"),
        title: payload.paused ? "the queue is paused" : "",
      }, mine.state === "running" ? "running now"
                                  : "#" + mine.position + " in the queue"));
    }).catch(function () { RF.dom.clear(host); });
  }

  var pending = null;
  function refresh() {
    clearTimeout(pending);
    return new Promise(function (resolve) {
      pending = setTimeout(function () {
        RF.api.get("/api/jobs/" + encodeURIComponent(state.id)).then(function (job) {
          state.job = job;
          renderHead(); renderRail(); renderPane();
          resolve(job);
        }).catch(function () { resolve(null); });
      }, 350);
    });
  }

  function select(stage) {
    state.stage = stage;
    history.replaceState(null, "", location.pathname + location.search + "#" + stage);
    renderRail();
    renderPane();
  }

  function init() {
    el = RF.dom.el; fmt = RF.dom.fmt;
    state.id = q("id");
    if (!state.id) {
      return RF.dom.mount(document.querySelector("[data-pane]"),
        el("div", { class: "empty" },
          el("div", { class: "empty__title" }, "No reel selected"),
          el("a", { class: "btn btn--primary", href: "/v2/" }, "Back to reels")));
    }
    RF.stages.refresh();

    RF.api.get("/api/jobs/" + encodeURIComponent(state.id)).then(function (job) {
      state.job = job;
      // the hash wins, then whatever needs attention, then the end of the line
      var wanted = location.hash.replace("#", "");
      var known = (job.stages || []).map(function (s) { return s.stage; });
      state.stage = known.indexOf(wanted) !== -1 ? wanted
                  : job.failed_stage || job.next_stage || known[known.length - 1];

      (job.log || []).slice(-60).forEach(note);
      renderHead(); renderRail(); renderPane();
      connect();
      queueChip();
      // the position only changes when something else in the queue does, so a
      // slow poll is enough -- the stage detail arrives over SSE
      setInterval(queueChip, 6000);
    }).catch(function (error) {
      RF.dom.mount(document.querySelector("[data-pane]"),
        el("div", { class: "callout callout--error" },
          el("div", null,
            el("div", { class: "callout__title" }, "That reel could not be loaded"),
            el("div", null, error.message),
            el("a", { class: "btn btn--sm", href: "/v2/" }, "Back to reels"))));
    });
  }

  RF.shell.ready(init);
})(window.RF || (window.RF = {}));
