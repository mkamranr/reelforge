/* The new-reel form.
 *
 * Grouped into sections rather than a flat column of ten fields, and every
 * screenshot is placed against a real reel frame before the job exists -- which
 * is the whole reason this page carries a preview rail.
 *
 * One behaviour kept deliberately from v1: the form reads the probe-free
 * /api/config/profiles for its defaults and never waits on
 * /api/config/providers, which does live network probes and once made this form
 * take 16 seconds to open. Reachability arrives afterwards and only annotates.
 */
(function (RF) {
  "use strict";

  var el;
  var state = { templates: [], profiles: null, roles: [], selected: null, frame: null };

  function field(label, control, hint) {
    return el("label", { class: "field" },
      el("span", { class: "field__label" }, label),
      control,
      hint ? el("span", { class: "field__hint" }, hint) : null);
  }

  function section(title, blurb, body) {
    return el("section", { class: "card stack", "data-gap": "4" },
      el("div", null,
        el("h2", { class: "card__title" }, title),
        blurb ? el("div", { class: "field__hint" }, blurb) : null),
      body);
  }

  /* ------------------------------------------------------------ the preview */
  function renderPreview() {
    var host = document.querySelector("[data-preview]");
    if (!host) return;
    var items = RF.stage.all();

    if (!items.length) {
      return RF.dom.mount(host, el("div", { class: "card stack", "data-gap": "3" },
        el("div", { class: "card__title" }, "Preview"),
        el("div", { class: "empty" },
          el("div", { class: "empty__art" }, RF.icons.reelArt()),
          el("div", { class: "empty__body" },
             "Add a screenshot and you will see exactly where it lands in the " +
             "reel — including whether the burned-in captions would sit on top of it."))));
    }

    var current = state.selected && items.filter(function (i) {
      return i.uid === state.selected;
    })[0];
    if (!current) { current = items[0]; state.selected = current.uid; }

    var frameHost = el("div");
    var warnings = el("div", { class: "stack", "data-gap": "2" });

    RF.dom.mount(host, el("div", { class: "card stack", "data-gap": "3" },
      el("div", { class: "cluster" },
        el("span", { class: "card__title grow" }, "In the reel"),
        items.length > 1 ? el("div", { class: "segmented" }, items.map(function (item, i) {
          return el("button", { type: "button", class: "segmented__item",
            "aria-pressed": String(item.uid === current.uid),
            onclick: function () { state.selected = item.uid; renderPreview(); } }, String(i + 1));
        })) : null),
      frameHost,
      placementControls(current),
      warnings));

    var frame = RF.frame.create(frameHost, {
      onChange: function (model) { paintWarnings(warnings, model, current); },
    });
    frame.setSource({ url: current.objectUrl, srcW: current.srcW, srcH: current.srcH });
    frame.setImage({ fit: current.fit, position: current.position, crop: current.crop,
                     eyebrow: eyebrowFor(current.role), captions: captionsOn() });
    frame.setPalette(paletteFor(templateValue()));
    frame.attachCropper({
      onCommit: function (model) {
        current.crop = { x: model.crop.x, y: model.crop.y, w: model.crop.w, h: model.crop.h };
      },
    });
    state.frame = frame;
  }

  function paintWarnings(host, model, item) {
    var found = RF.spec.collisions({
      fit: model.fit, position: model.position, crop: model.crop,
      srcW: item.srcW, srcH: item.srcH,
    }, captionsOn());
    RF.dom.mount(host, found.map(function (warning) {
      return el("div", { class: "callout callout--" + (warning.level === "warn" ? "warn" : "info") },
        el("div", null, warning.message));
    }));
  }

  function placementControls(item) {
    return el("div", { class: "stack", "data-gap": "3" },
      el("div", { class: "cluster", "data-gap": "2" },
        el("span", { class: "field__label" }, "Fit"),
        el("div", { class: "segmented" },
          [["panel", "Panel — full width"], ["full", "Full bleed 9:16"]].map(function (pair) {
            return el("button", { type: "button", class: "segmented__item",
              "aria-pressed": String(item.fit === pair[0]),
              onclick: function () {
                item.fit = pair[0];
                item.crop = RF.spec.defaultCrop(item.srcW, item.srcH, item.fit);
                renderPreview();
              } }, pair[1]);
          })),
        el("span", { class: "grow" }),
        el("span", { class: "field__label" }, "Position"),
        el("div", { class: "segmented" },
          ["top", "centre", "bottom"].map(function (pos) {
            return el("button", { type: "button", class: "segmented__item",
              disabled: item.fit === "full",
              title: item.fit === "full"
                ? "A full-bleed image fills the frame, so it has nowhere to move"
                : null,
              "aria-pressed": String(item.position === pos),
              onclick: function () { item.position = pos; renderPreview(); } }, pos);
          }))),
      el("div", { class: "cluster", "data-gap": "2" },
        el("select", { class: "select", style: null,
          onchange: function (event) { item.role = event.target.value; renderPreview(); } },
          state.roles.map(function (role) {
            return el("option", { value: role.key, selected: item.role === role.key },
                      role.label);
          })),
        el("input", { class: "input grow", placeholder: "Caption (optional)",
          value: item.caption,
          oninput: function (event) { item.caption = event.target.value.slice(0, 44); } }),
        el("button", { type: "button", class: "btn btn--sm btn--danger",
          onclick: function () {
            RF.stage.remove(item.uid); state.selected = null;
            renderPreview(); renderScreenshots();
          } }, "Remove")));
  }

  function eyebrowFor(role) {
    var spec = RF.spec.get();
    return (spec.roles && spec.roles[role]) || "A LOOK";
  }
  function paletteFor(name) {
    var found = state.templates.filter(function (t) { return t.name === name; })[0];
    return found && found.palette;
  }
  function captionsOn() {
    var box = document.querySelector("[name=captions]");
    return box ? box.checked : true;
  }
  function templateValue() {
    var select = document.querySelector("[name=template]");
    return select ? select.value : "";
  }

  /* ------------------------------------------------------------ the fields */
  function renderScreenshots() {
    var host = document.querySelector("[data-shots]");
    if (!host) return;
    var items = RF.stage.all();
    RF.dom.mount(host, el("div", { class: "stack", "data-gap": "3" },
      state.roles.map(function (role) {
        var mine = items.filter(function (i) { return i.role === role.key; });
        return el("div", { class: "cluster", "data-align": "start" },
          el("div", { class: "grow" },
            el("div", { class: "field__label" }, role.label),
            el("div", { class: "field__hint" }, role.hint)),
          el("label", { class: "btn btn--sm" }, RF.icon("upload", { size: 14 }), "Choose",
            el("input", { type: "file", accept: "image/png,image/jpeg,image/webp",
              multiple: "multiple", hidden: true,
              onchange: function (event) { take(event.target.files, role.key); } })),
          mine.length ? el("span", { class: "badge" }, mine.length) : null);
      })));
  }

  function take(files, role) {
    RF.stage.add(files, role).then(function (result) {
      result.rejected.forEach(function (bad) {
        RF.dom.toast(bad.name + ": " + bad.reason, { kind: "error", timeout: 8000 });
      });
      if (result.added.length) state.selected = result.added[0].uid;
      renderScreenshots(); renderPreview();
    });
  }

  function renderForm() {
    var form = document.querySelector("[data-form]");
    var profiles = state.profiles || {};
    var llm = (profiles.llm || {}).providers || [];
    var tts = (profiles.tts || {}).providers || [];

    RF.dom.mount(form, [
      el("h1", { class: "section__title" }, "New reel"),

      section("Source", "A GitHub repository or a Hugging Face model.",
        field("URL",
          el("input", { class: "input", name: "url", required: "required",
                        placeholder: "https://github.com/owner/repo", autofocus: "autofocus" }))),

      section("Look", "The palette and motion the reel is built from.",
        field("Template",
          el("select", { class: "select", name: "template",
                         onchange: function () {
                           if (state.frame) state.frame.setPalette(paletteFor(templateValue()));
                         } },
            state.templates.map(function (t) {
              return el("option", { value: t.name }, t.title + " — " + (t.description || "").slice(0, 60));
            })))),

      section("Script", null, el("div", { class: "stack", "data-gap": "4" },
        el("label", { class: "switch" },
          el("input", { type: "checkbox", name: "captions",
                        checked: profiles.burn_captions !== false,
                        onchange: renderPreview }),
          el("span", { class: "switch__track" }),
          el("span", null, "Burn word-synced captions into the video")),
        el("div", { class: "field__hint" },
           "Instagram, YouTube and Facebook auto-caption on upload, so this " +
           "duplicates them — but burned-in text is what carries a muted autoplay."),
        field("Fact checking",
          el("select", { class: "select", name: "fact_check" },
            [["strict", "Strict — a figure that does not trace goes back to the model"],
             ["warn", "Warn — let the draft through and flag it"],
             ["off", "Off — do not check"]].map(function (pair) {
              return el("option", { value: pair[0],
                selected: (profiles.fact_check || "strict") === pair[0] }, pair[1]);
            })),
          "Every figure on screen and in the narration must trace to the API or " +
          "to the project's own README."),
        field("Generated visuals",
          el("select", { class: "select", name: "visuals" },
            [["", "use configured (" + (((profiles.visuals || {}).enabled)
                 ? "on: " + (profiles.visuals || {}).selected : "off") + ")"],
             ["on", "On: stills and a clip from the script, a backdrop under the cover"],
             ["music", "On, with a music bed under the narration"],
             ["off", "Off: the renderer draws everything"]].map(function (pair) {
              return el("option", { value: pair[0] }, pair[1]);
            })),
          "Needs a ComfyUI profile under Settings. Generation is slow: a clip is " +
          "minutes on a GPU box."))),

      section("Model and voice", "Leave these alone to use the configured defaults.",
        el("div", { class: "stack", "data-gap": "3" },
          field("Script model",
            el("select", { class: "select", name: "llm_provider" },
              [el("option", { value: "" }, "use configured (" + ((profiles.llm || {}).selected || "") + ")")]
                .concat(llm.map(function (p) {
                  return el("option", { value: p.provider },
                            p.provider + (p.model ? " — " + p.model : ""));
                })))),
          field("Voice",
            el("select", { class: "select", name: "tts_provider" },
              [el("option", { value: "" }, "use configured (" + ((profiles.tts || {}).selected || "") + ")")]
                .concat(tts.map(function (p) {
                  return el("option", { value: p.provider }, p.provider);
                })))))),

      section("Screenshots", "Optional. With none, the reel is exactly what it " +
                             "would have been — the layout that needs one is never chosen.",
        el("div", { "data-shots": "" })),

      section("Approval", null,
        field("Pause for review",
          el("select", { class: "select", name: "gates" },
            [["", "Use the saved default"],
             ["none", "Run unattended — finish without stopping"],
             ["all", "Pause after every stage"]].map(function (pair) {
              return el("option", { value: pair[0] }, pair[1]);
            })),
          "The saved default pauses at content and storyboard, so the reel stops "
          + "twice before it renders anything and waits for you. Queue several "
          + "that way and you come back to all of them parked at the first gate — "
          + "pick “run unattended” for a batch you want finished.")),

      el("div", { class: "cluster" },
        el("button", { type: "submit", class: "btn btn--primary" },
           RF.icon("play", { size: 15 }), "Create and start"),
        el("a", { class: "btn btn--ghost", href: "/v2/" }, "Cancel"),
        el("span", { class: "field__hint", "data-progress": "" })),
    ]);

    renderScreenshots();
    form.addEventListener("submit", submit);
  }

  var STAGES = ["ingest", "content", "cover", "audio", "align",
                "storyboard", "render", "verify", "package"];

  function submit(event) {
    event.preventDefault();
    var form = event.target;
    var button = form.querySelector("button[type=submit]");
    var progress = form.querySelector("[data-progress]");
    var value = function (name) {
      var node = form.querySelector("[name=" + name + "]");
      return node ? node.value : "";
    };

    var gates = value("gates");
    var body = {
      url: value("url").trim(),
      template: value("template"),
      captions: captionsOn(),
      fact_check: value("fact_check"),
      llm_provider: value("llm_provider") || null,
      tts_provider: value("tts_provider") || null,
      visuals: value("visuals") === "" ? null
        : { enabled: value("visuals") !== "off",
            music: value("visuals") === "music" ? true : null },
      manual_stages: gates === "" ? null : (gates === "all" ? STAGES : []),
      // With screenshots staged the job is created stopped, so they can be
      // uploaded before the storyboard is written -- it picks one layout per
      // image, and an image that arrives later never appears.
      autostart: RF.stage.count() === 0,
    };

    button.dataset.busy = "1";
    button.textContent = "Creating…";

    RF.api.post("/api/jobs", body).then(function (job) {
      if (!RF.stage.count()) return job;
      return RF.stage.commit(job.id, function (done, total) {
        progress.textContent = "uploading screenshot " + done + " of " + total + "…";
      }).then(function (result) {
        result.failed.forEach(function (bad) {
          RF.dom.toast(bad.name + ": " + bad.reason, { kind: "error", timeout: 9000 });
        });
        progress.textContent = "starting…";
        return RF.api.post("/api/jobs/" + encodeURIComponent(job.id) + "/run", {})
          .then(function () { return job; });
      });
    }).then(function (job) {
      RF.stage.clear();
      location.href = "/v2/job.html?id=" + encodeURIComponent(job.id);
    }).catch(function (error) {
      delete button.dataset.busy;
      button.textContent = "Create and start";
      progress.textContent = "";
      RF.dom.mount(form.querySelector("[data-progress]"), "");
      RF.dom.toast(error.message, { kind: "error", timeout: 12000 });
    });
  }

  function init() {
    el = RF.dom.el;
    Promise.all([
      RF.api.get("/api/templates"),
      RF.api.get("/api/config/profiles"),
      RF.api.get("/api/jobs/images/roles"),
      RF.spec.load(),
    ]).then(function (results) {
      state.templates = results[0] || [];
      state.profiles = results[1] || {};
      state.roles = (results[2] || {}).roles || [];
      renderForm();
      renderPreview();
    }).catch(function (error) {
      RF.dom.mount(document.querySelector("[data-form]"),
        el("div", { class: "callout callout--error" },
          el("div", null,
            el("div", { class: "callout__title" }, "This form could not load"),
            el("div", null, error.message))));
    });
  }

  RF.shell.ready(init);
})(window.RF || (window.RF = {}));
