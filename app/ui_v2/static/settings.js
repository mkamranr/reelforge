/* Settings: providers, model routing, gates and keys.
 *
 * Every endpoint here is behind require_admin (loopback, or a bearer token), so
 * a 403 is a normal answer rather than an error -- it means "you are not on the
 * machine this runs on", and the page says so instead of showing a stack.
 */
(function (RF) {
  "use strict";

  var el;

  /* Each profile carries its own `fields` schema -- key, label, type, help --
   * so the form is generated from what the adapter actually accepts rather than
   * a hardcoded case per adapter. A new adapter gets an editor for free. */
  var KIND_TITLES = { llm: "Language models", tts: "Voices", visuals: "Pictures" };
  var KIND_NOUNS = { llm: "model", tts: "voice", visuals: "picture source" };

  function editProfile(kind, profile, adapters) {
    var isNew = !profile;
    profile = profile || { adapter: adapters[0], settings: {}, fields: [] };
    var values = Object.assign({}, profile.settings || {});
    var inputs = {};

    var nameInput = el("input", { class: "input", value: profile.name || "",
                                  disabled: !isNew, placeholder: "a short name" });
    var adapterSelect = el("select", { class: "select", disabled: !isNew },
      adapters.map(function (a) {
        return el("option", { value: a, selected: a === profile.adapter }, a);
      }));

    var fieldHost = el("div", { class: "stack", "data-gap": "3" });

    /* The schema says what each field *is* -- select, bool, keyname, voice,
     * number, text -- and rendering everything as a text box throws that away.
     * "Structured output" is three fixed values; typing one is a way to get it
     * wrong. */
    function drawFields(fields) {
      inputs = {};
      RF.dom.mount(fieldHost, (fields || []).map(function (field) {
        var value = values[field.key];
        var control = buildControl(kind, field, value, profile);
        inputs[field.key] = control;
        return el("label", { class: "field" },
          el("span", { class: "field__label" }, field.label || field.key),
          control.node,
          field.help ? el("span", { class: "field__hint" }, field.help) : null);
      }));
    }
    drawFields(profile.fields);

    /* Changing the adapter changes which fields exist, so the form follows it
     * rather than showing the previous adapter's shape. */
    adapterSelect.addEventListener("change", function () {
      var match = (window.__RF_PROFILES || []).filter(function (p) {
        return p.adapter === adapterSelect.value;
      })[0];
      drawFields(match ? match.fields : []);
    });

    var handle = RF.dom.dialog(RF.dom.frag(
      el("div", { class: "dialog__head" },
        el("h2", { class: "card__title" },
           isNew ? "New " + (KIND_NOUNS[kind] || kind) + " profile"
                 : "Edit " + profile.name)),
      el("div", { class: "dialog__body stack", "data-gap": "4" },
        el("label", { class: "field" },
          el("span", { class: "field__label" }, "Name"),
          nameInput,
          el("span", { class: "field__hint" },
             isNew ? "Letters, digits, - and _."
                   : "A profile's name and adapter cannot change: its settings mean "
                     + "different things to different adapters, and reinterpreting a "
                     + "base_url silently is worse than making a new one.")),
        el("label", { class: "field" },
          el("span", { class: "field__label" }, "Adapter"),
          adapterSelect),
        fieldHost),
      el("div", { class: "dialog__foot" },
        el("button", { class: "btn btn--ghost",
                       onclick: function () { handle.close(); } }, "Cancel"),
        el("button", { class: "btn btn--primary", onclick: function () {
          var name = nameInput.value.trim();
          if (!name) return;
          var settings = {};
          Object.keys(inputs).forEach(function (key) {
            var read = inputs[key].read();
            if (read === "" || read === null || read === undefined) return;
            settings[key] = read;
          });
          handle.close();
          RF.api.put("/api/settings/" + kind + "/profiles/" + encodeURIComponent(name), {
            adapter: adapterSelect.value,
            model: settings.model || "",
            base_url: settings.base_url || null,
            api_key_env: settings.api_key_env || null,
            settings: settings,
          })
            .then(function () { RF.dom.toast(name + " saved", { kind: "ok" }); load(); })
            .catch(function (error) {
              RF.dom.toast(error.message, { kind: "error", timeout: 12000 });
            });
        } }, "Save"))
    ), { label: isNew ? "New profile" : "Edit " + profile.name });
    nameInput.focus();
  }

  /* One control per declared type. `read()` is what the caller uses, so the
   * dialog never has to know which widget a field turned into. */
  function buildControl(kind, field, value, profile) {
    if (field.type === "select") {
      var select = el("select", { class: "select" },
        (field.options || []).map(function (option) {
          return el("option", { value: option, selected: String(value) === option }, option);
        }));
      return { node: select, read: function () { return select.value; } };
    }

    if (field.type === "bool") {
      var box = el("input", { type: "checkbox", checked: value === true });
      return {
        node: el("span", { class: "switch" }, box, el("span", { class: "switch__track" })),
        read: function () { return box.checked; },
      };
    }

    if (field.type === "voice") {
      // Populated from the provider itself when it can be reached; a plain text
      // box otherwise, because a voice id typed by hand still works and an
      // unreachable provider must not block editing the rest of the profile.
      var input = el("input", { class: "input", value: value == null ? "" : String(value),
                                list: "voices-" + field.key });
      var list = el("datalist", { id: "voices-" + field.key });
      var wrap = el("span", { class: "field__control" }, input, list);
      if (profile && profile.name) {
        RF.api.get("/api/settings/" + kind + "/profiles/" +
                   encodeURIComponent(profile.name) + "/voices")
          .then(function (result) {
            RF.dom.mount(list, (result.voices || []).map(function (voice) {
              return el("option", { value: voice.id || voice.voice_id || voice },
                        voice.name || "");
            }));
          })
          .catch(function () { /* unreachable provider; the text box still works */ });
      }
      return { node: wrap, read: function () { return input.value.trim(); } };
    }

    if (field.type === "audio") {
      // A file that lives on the server, not a string: upload it here and the
      // profile's setting becomes the stored path. Needs a saved profile to
      // attach to, so a brand-new one says so instead of offering a button
      // that cannot work yet.
      var name = profile && profile.name;
      var current = value ? String(value).split(/[\\/]/).pop() : "";
      var status = el("span", { class: "field__hint" },
                      current ? "sample: " + current : "no sample -- the adapter will fix a voice itself");
      var player = current && name
        ? el("audio", { controls: "controls", preload: "none", class: "shot",
                        src: "/api/settings/tts/profiles/" + encodeURIComponent(name) + "/reference" })
        : null;
      var picker = el("input", { type: "file", accept: ".wav,.mp3,.m4a,.flac,.ogg,.aac" });
      var transcript = el("input", { class: "input", placeholder: "what the sample says (optional, improves cloning)" });
      var uploadBtn = el("button", { class: "btn btn--sm", type: "button", onclick: function () {
        if (!name) { RF.dom.toast("save the profile first, then upload a sample", { kind: "error" }); return; }
        if (!picker.files || !picker.files[0]) { RF.dom.toast("choose an audio file first", { kind: "error" }); return; }
        var form = new FormData();
        form.append("file", picker.files[0]);
        if (transcript.value.trim()) form.append("transcript", transcript.value.trim());
        uploadBtn.dataset.busy = "1";
        RF.api.upload("/api/settings/tts/profiles/" + encodeURIComponent(name) + "/reference", form)
          .then(function (result) {
            RF.dom.toast("sample voice saved: " + ((result.reference || {}).file || ""), { kind: "ok" });
            load();
          })
          .catch(function (error) { RF.dom.toast(error.message, { kind: "error" }); })
          .then(function () { delete uploadBtn.dataset.busy; });
      } }, "Upload sample");
      var removeBtn = current && name ? el("button", { class: "btn btn--sm btn--quiet", type: "button",
        onclick: function () {
          RF.api.del("/api/settings/tts/profiles/" + encodeURIComponent(name) + "/reference")
            .then(function () { RF.dom.toast("sample removed", { kind: "ok" }); load(); })
            .catch(function (error) { RF.dom.toast(error.message, { kind: "error" }); });
        } }, "Remove") : null;
      var wrap = el("span", { class: "stack", "data-gap": "2" },
        status, player, picker, transcript,
        el("span", { class: "cluster", "data-gap": "2" }, uploadBtn, removeBtn));
      // the path itself is not typed here; the form contributes nothing for
      // it, so a dialog opened before the upload cannot overwrite the upload
      return { node: wrap, read: function () { return undefined; } };
    }

    if (field.type === "number") {
      var number = el("input", { class: "input", type: "number", step: field.step || "any",
                                 value: value == null ? "" : String(value) });
      return {
        node: number,
        read: function () {
          var raw = number.value.trim();
          return raw === "" ? null : Number(raw);
        },
      };
    }

    // keyname and text. keyname offers the variables already in use, since a
    // key saved under a name no profile references does nothing.
    var text = el("input", { class: "input", value: value == null ? "" : String(value),
                             list: field.type === "keyname" ? "known-keynames" : null,
                             placeholder: field.type === "keyname" ? "SOMETHING_API_KEY" : null });
    return { node: text, read: function () { return text.value.trim(); } };
  }

  function removeProfile(kind, profile) {
    RF.dom.confirm({
      title: "Delete " + profile.name + "?",
      body: "Any reel configured to use it falls back to whichever profile is active.",
      confirmLabel: "Delete", danger: true,
    }).then(function (yes) {
      if (!yes) return;
      RF.api.del("/api/settings/" + kind + "/profiles/" + encodeURIComponent(profile.name))
        .then(function () { RF.dom.toast(profile.name + " deleted", { kind: "ok" }); load(); })
        .catch(function (error) {
          RF.dom.toast(error.message, { kind: "error", timeout: 10000 });
        });
    });
  }

  function profileList(kind, block, adapters) {
    var profiles = block.profiles || [];
    return el("div", { class: "card card--flush" },
      el("div", { class: "card__head" },
        el("span", { class: "card__title grow" }, KIND_TITLES[kind] || kind),
        el("span", { class: "badge" }, "active: " + (block.active || "none")),
        el("button", { class: "btn btn--sm",
          onclick: function () { editProfile(kind, null, adapters); } },
           RF.icon("plus", { size: 14 }), "Add")),
      el("table", { class: "table" },
        el("thead", null, el("tr", null,
          el("th", null, "profile"), el("th", null, "adapter"),
          el("th", null, "model"), el("th", null, "key"), el("th", null, ""))),
        el("tbody", null, profiles.map(function (p) {
          var on = p.name === block.active;
          var key = p.key || {};
          return el("tr", null,
            el("th", null, p.name, " ",
               on ? el("span", { class: "chip chip--done" }, "active") : null),
            el("td", { class: "muted" }, p.adapter || ""),
            el("td", { class: "mono truncate" }, p.model || ""),
            el("td", null, p.api_key_env
              ? el("span", { class: "field__hint" },
                   key.state === "set" ? (key.masked || "set") : "not set")
              : el("span", { class: "field__hint" }, "none needed")),
            el("td", { class: "table__num" },
              el("button", { class: "iconbtn", title: "Test this profile",
                onclick: function (event) { test(kind, p.name, event.currentTarget); },
              }, RF.icon("refresh", { size: 15 })),
              el("button", { class: "iconbtn", title: "Edit",
                onclick: function () { editProfile(kind, p, adapters); },
              }, RF.icon("settings", { size: 15 })),
              el("button", { class: "iconbtn", title: "Delete",
                onclick: function () { removeProfile(kind, p); },
              }, RF.icon("trash", { size: 15 })),
              on ? null : el("button", { class: "btn btn--sm",
                onclick: function () { activate(kind, p.name); } }, "Use")));
        }))));
  }

  function activate(kind, name) {
    // `name` is an embedded Body field, not a query parameter -- sending it in
    // the query returns "name: Field required", which reads like the UI failed
    // to pass a name at all rather than passing it in the wrong place.
    RF.api.post("/api/settings/" + kind + "/active", { name: name })
      .then(function () { RF.dom.toast(name + " is now active", { kind: "ok" }); load(); })
      .catch(function (error) { RF.dom.toast(error.message, { kind: "error" }); });
  }

  function test(kind, name, button) {
    button.dataset.busy = "1";
    RF.api.post("/api/settings/" + kind + "/profiles/" + encodeURIComponent(name) + "/test", {})
      .then(function (result) {
        // The probe answers `reachable` and, separately, `authenticated` --
        // reading a non-existent `ok` reported every provider as unreachable,
        // including the two that answered. They are separate questions: a local
        // vLLM needs no key, and a hosted one can be up and still refuse you.
        var message;
        if (!result.reachable) {
          message = "not reachable — " + (result.error || "no answer") +
                    (result.base_url ? " (" + result.base_url + ")" : "");
        } else if (result.authenticated === false) {
          message = "reachable, but the key was refused" +
                    (result.api_key_env ? " — check " + result.api_key_env : "");
        } else if (result.error) {
          // reachable, but something about the workflows is wrong -- the
          // server answered, so say what it said rather than "unreachable"
          message = "reachable, but: " + result.error;
        } else {
          message = "reachable" +
                    (result.available_models && result.available_models.length
                       ? " — " + result.available_models.length + " models" : "") +
                    (result.devices && result.devices.length
                       ? " — " + result.devices.map(function (d) {
                           return (d.name || "gpu") + (d.vram_gb ? " " + d.vram_gb + " GB" : "");
                         }).join(", ") : "") +
                    (result.note ? " — " + result.note : "");
        }
        RF.dom.toast(name + ": " + message,
                     { kind: result.reachable && result.authenticated !== false && !result.error
                             ? "ok" : "error", timeout: 10000 });
      })
      .catch(function (error) {
        RF.dom.toast(name + ": " + error.message, { kind: "error", timeout: 9000 });
      })
      .then(function () { delete button.dataset.busy; });
  }

  /* Which model does what. Worth its own control rather than a read-only
   * table: the whole point of roles is that storyboard codegen wants the
   * strongest model available while the cheap stages do not, and that is a
   * decision people revise. */
  function rolesCard(settings) {
    var block = settings.llm || {};
    var roles = block.roles || {};
    var names = block.role_names || Object.keys(roles);
    var profiles = block.profiles || [];
    if (!names.length) return null;

    var picks = {};

    function save() {
      var payload = {};
      names.forEach(function (role) {
        var value = picks[role].value;
        payload[role] = value ? { profile: value } : {};
      });
      RF.api.put("/api/settings/llm/roles", payload)
        .then(function () { RF.dom.toast("routing saved", { kind: "ok" }); load(); })
        .catch(function (error) {
          RF.dom.toast(error.message, { kind: "error", timeout: 10000 });
        });
    }

    return el("div", { class: "card stack", "data-gap": "4" },
      el("div", null,
        el("div", { class: "card__title" }, "Which model does what"),
        el("div", { class: "field__hint" },
           "Left unset, a stage uses whichever profile is active. Storyboard " +
           "codegen is the one worth pointing at the strongest model you have.")),

      el("div", { class: "stack", "data-gap": "3" },
        names.map(function (role) {
          var current = (roles[role] || {}).profile || "";
          var select = el("select", { class: "select", onchange: save },
            [el("option", { value: "" }, "use the active profile (" +
                                          (block.active || "none") + ")")]
              .concat(profiles.map(function (p) {
                return el("option", { value: p.name, selected: p.name === current },
                          p.name + (p.model ? " — " + p.model : ""));
              })));
          picks[role] = select;
          return el("label", { class: "field" },
            el("span", { class: "field__label" }, RF.stages.label(role)),
            select);
        })),

      /* `presets` is an object keyed by name, each carrying a label and a
       * sentence explaining the trade -- which is the useful part. Treating it
       * as an array threw the explanation away and then failed on .map. */
      el("div", { class: "stack", "data-gap": "2" },
        el("span", { class: "field__hint" }, "Or start from a preset"),
        el("div", { class: "grid", dataset: { min: "220" } },
          Object.keys(block.presets || {}).map(function (key) {
            var preset = block.presets[key] || {};
            return el("button", { class: "choicecard", type: "button",
              onclick: function () { applyPreset(key, preset.label || key); } },
              el("span", { class: "choicecard__title" }, preset.label || key),
              preset.description
                ? el("span", { class: "choicecard__body" }, preset.description) : null);
          }))));
  }

  function applyPreset(key, label) {
    RF.api.post("/api/settings/llm/roles/preset/" + encodeURIComponent(key), {})
      .then(function () { RF.dom.toast(label + " applied", { kind: "ok" }); load(); })
      .catch(function (error) {
        RF.dom.toast(error.message, { kind: "error", timeout: 10000 });
      });
  }

  /* ElevenLabs rotates keys on its own when one runs out of credit; this is for
   * choosing one deliberately, or clearing the pin to let it rotate again. */
  function ttsKeyCard(settings) {
    var tts = settings.tts || {};
    var active = (tts.profiles || []).filter(function (p) {
      return p.name === tts.active;
    })[0];
    if (!active || active.adapter !== "elevenlabs") return null;
    var keys = (active.credits && active.credits.keys) || active.keys;
    if (!keys || !keys.length) return null;

    return el("div", { class: "card stack", "data-gap": "3" },
      el("div", { class: "card__title" }, "ElevenLabs keys"),
      el("div", { class: "field__hint" },
         "A key that runs out of credit is rotated past automatically. Pin one " +
         "to use it deliberately, or clear the pin to rotate again."),
      el("div", { class: "cluster", "data-gap": "2" },
        keys.map(function (key, index) {
          var label = key.label || key.name || ("key " + (index + 1));
          return el("button", { class: "btn btn--sm" + (key.pinned ? " btn--primary" : ""),
            onclick: function () { pinKey(label); } },
            label + (key.remaining !== undefined ? " · " + key.remaining + " left" : ""));
        }),
        el("button", { class: "btn btn--sm btn--quiet",
          onclick: function () { pinKey(null); } }, "Rotate automatically")));
  }

  function pinKey(label) {
    var path = "/api/config/tts/key" + (label ? "?label=" + encodeURIComponent(label) : "");
    RF.api.post(path, {})
      .then(function () {
        RF.dom.toast(label ? "pinned to " + label : "rotating automatically", { kind: "ok" });
        load();
      })
      .catch(function (error) { RF.dom.toast(error.message, { kind: "error" }); });
  }

  var STAGES = ["ingest", "content", "cover", "audio", "align",
                "storyboard", "render", "verify", "package"];

  /* How much generated imagery a reel gets. A separate card from the profile
   * list because these are not properties of a server: two stills and a clip
   * is a decision about the reel, whichever box draws them. */
  /* The admin token, for a ReelForge published beyond localhost.
   *
   * This card has to work when the rest of the page could not load: the token
   * is what unlocks GET /api/settings, so on a 401 it is the only thing
   * rendered. Hence `access` takes no arguments and never reads `settings`.
   */
  function accessCard(note) {
    var input = el("input", { class: "input", type: "password",
                              autocomplete: "off", placeholder: "paste the admin token",
                              value: RF.api.getToken() ? "................" : "" });
    var save = el("button", { class: "btn btn--primary btn--sm", type: "button" }, "Save");
    var forget = el("button", { class: "btn btn--sm", type: "button" }, "Forget");

    save.addEventListener("click", function () {
      var value = input.value.trim();
      if (!value || value === "................") {
        RF.dom.toast("paste the token first", { kind: "error" });
        return;
      }
      RF.api.setToken(value);
      RF.dom.toast("token saved in this browser", { kind: "ok" });
      load();
    });
    forget.addEventListener("click", function () {
      RF.api.setToken("");
      RF.dom.toast("token forgotten", { kind: "ok" });
      load();
    });

    return el("div", { class: "card" },
      el("div", { class: "card__title" }, "Access"),
      el("div", { class: "field__hint" },
         note || ("This ReelForge is reachable beyond its own machine, so the "
                  + "settings API asks for the admin token -- the value of "
                  + "REELFORGE_ADMIN_TOKEN on the server. It is kept in this "
                  + "browser only and is never saved as a setting.")),
      el("div", { class: "cluster", "data-gap": "2" }, input, save,
         RF.api.getToken() ? forget : null));
  }

  function visualsCard(settings) {
    var block = settings.visuals || {};
    var options = block.options || {};
    var enabled = !!block.enabled;

    function save(patch, message) {
      return RF.api.put("/api/settings/visuals/options", patch)
        .then(function () { RF.dom.toast(message || "saved", { kind: "ok" }); load(); })
        .catch(function (error) { RF.dom.toast(error.message, { kind: "error" }); });
    }

    function number(key, label, min, max, step) {
      var input = el("input", { class: "input", type: "number", min: String(min),
                                max: String(max), step: String(step || 1),
                                value: String(options[key] == null ? "" : options[key]) });
      input.addEventListener("change", function () {
        var patch = {};
        patch[key] = step && step < 1 ? parseFloat(input.value) : parseInt(input.value, 10);
        save(patch, label + " saved");
      });
      return el("label", { class: "field" },
        el("span", { class: "field__label" }, label), input);
    }

    var coverBox = el("input", { type: "checkbox", checked: !!options.cover });
    coverBox.addEventListener("change", function () {
      save({ cover: coverBox.checked }, "cover backdrop " + (coverBox.checked ? "on" : "off"));
    });
    var fitSelect = el("select", { class: "select" },
      ["full", "panel"].map(function (v) {
        return el("option", { value: v, selected: v === options.still_fit }, v);
      }));
    fitSelect.addEventListener("change", function () { save({ still_fit: fitSelect.value }); });
    var musicBox = el("input", { type: "checkbox", checked: !!options.music });
    musicBox.addEventListener("change", function () {
      save({ music: musicBox.checked }, "music bed " + (musicBox.checked ? "on" : "off"));
    });
    var sfxBox = el("input", { type: "checkbox", checked: !!options.sfx_samples });
    sfxBox.addEventListener("change", function () {
      save({ sfx_samples: sfxBox.checked },
           "cut sounds " + (sfxBox.checked ? "from generated samples" : "synthesized"));
    });
    var styleInput = el("textarea", { class: "input", rows: "2" }, options.style || "");
    var negativeInput = el("input", { class: "input", value: options.negative || "",
                                      placeholder: "empty keeps the workflow's own" });

    return el("div", { class: "card stack", "data-gap": "3" },
      el("div", { class: "card__title" }, "Pictures: how many, and the look"),
      el("div", { class: "field__hint" },
         enabled
           ? "Active profile: " + block.active + ". Each reel gets stills from its " +
             "scenes' on-screen directions, a clip from the scene with a shot " +
             "description, and a backdrop under the cover's typography. Counts " +
             "are ceilings; a short script gets fewer."
           : "Off. Add a ComfyUI profile above and press Use to turn generated " +
             "imagery on. Until then every reel is drawn by the renderer alone, " +
             "exactly as before."),
      el("div", { class: "cluster", "data-gap": "3" },
        number("stills", "Stills per reel", 0, 6),
        number("clips", "Clips per reel", 0, 3),
        number("clip_seconds", "Clip length (s)", 2, 10, 0.5)),
      el("div", { class: "cluster", "data-gap": "3" },
        el("label", { class: "field" },
          el("span", { class: "field__label" }, "Cover backdrop"),
          el("span", { class: "cluster", "data-gap": "2" }, coverBox,
             el("span", { class: "field__hint" }, "paint the cover over a generated image"))),
        el("label", { class: "field" },
          el("span", { class: "field__label" }, "Still placement"), fitSelect,
          el("span", { class: "field__hint" },
             "full bleeds to the frame edge under a scrim; panel sits in a card."))),
      el("div", { class: "cluster", "data-gap": "3" },
        el("label", { class: "field" },
          el("span", { class: "field__label" }, "Music bed"),
          el("span", { class: "cluster", "data-gap": "2" }, musicBox,
             el("span", { class: "field__hint" },
                "an instrumental bed from the audio workflow, ducked under the voice"))),
        number("music_gain_db", "Bed level (dB)", -40, -6),
        number("music_duck_db", "Duck under voice (dB)", -24, 0),
        el("label", { class: "field" },
          el("span", { class: "field__label" }, "Cut sounds"),
          el("span", { class: "cluster", "data-gap": "2" }, sfxBox,
             el("span", { class: "field__hint" },
                "generated one-shots instead of the synthesized set; made once per profile")))),
      el("div", { class: "field__hint" },
         "Stable Audio makes music and sound effects, not speech: narration still comes " +
         "from a voice profile or an upload."),
      el("label", { class: "field" },
        el("span", { class: "field__label" }, "Style suffix, added to every prompt"), styleInput,
        el("span", { class: "field__hint" },
           "Palette words from the template are added automatically.")),
      el("label", { class: "field" },
        el("span", { class: "field__label" }, "Negative prompt override"), negativeInput),
      el("div", { class: "cluster", "data-gap": "2" },
        el("button", { class: "btn btn--sm", onclick: function () {
          save({ style: styleInput.value, negative: negativeInput.value }, "style saved");
        } }, "Save style")));
  }

  function gatesCard(settings) {
    var manual = (((settings.approval || {}).manual_stages) || []).slice();

    function setGates(stages, message) {
      return RF.api.put("/api/settings/approval", { manual_stages: stages })
        .then(function () { RF.dom.toast(message, { kind: "ok" }); load(); })
        .catch(function (error) { RF.dom.toast(error.message, { kind: "error" }); });
    }

    function toggle(stage) {
      var index = manual.indexOf(stage);
      if (index === -1) manual.push(stage); else manual.splice(index, 1);
      setGates(manual, "gates saved");
    }

    return el("div", { class: "card stack", "data-gap": "3" },
      el("div", { class: "card__title" }, "Review gates"),
      el("div", { class: "field__hint" },
         "A reel pauses after each of these and waits for you. It matters more " +
         "with a queue: a queued reel that stops at a gate releases the slot and " +
         "lets the next one start, so five reels with two gates each drain in " +
         "minutes into five reels awaiting review."),
      el("div", { class: "cluster", "data-gap": "2" },
        STAGES.map(function (stage) {
          var on = manual.indexOf(stage) !== -1;
          return el("button", {
            class: "chip " + (on ? "chip--review" : ""),
            "aria-pressed": String(on),
            title: on ? "Pausing here — click to run straight through"
                      : "Running straight through — click to pause here",
            onclick: function () { toggle(stage); },
          }, RF.stages.label(stage));
        })),
      el("div", { class: "cluster", "data-gap": "2" },
        el("button", { class: "btn btn--sm btn--quiet", onclick: function () {
          setGates([], "gates cleared");
        } }, "Run straight through"),
        el("button", { class: "btn btn--sm btn--quiet", onclick: function () {
          setGates(STAGES.slice(), "pausing at every stage");
        } }, "Pause everywhere")));
  }

  function secretsCard(rows) {
    rows = rows || [];
    return el("div", { class: "card card--flush" },
      el("div", { class: "card__head" },
        el("span", { class: "card__title grow" }, "API keys"),
        el("span", { class: "field__hint" }, "stored locally, never sent back")),
      rows.length
        ? el("table", { class: "table table--dense" },
            el("tbody", null, rows.map(function (row) {
              return el("tr", null,
                el("th", { class: "mono" }, row.name),
                el("td", null,
                   row.state === "set"
                     ? el("span", { class: "chip chip--done" },
                          row.masked || "set")
                     : el("span", { class: "muted" }, "not set"),
                   row.source ? el("span", { class: "field__hint" },
                                   " from " + row.source) : null,
                   /* A key set here does nothing while an environment variable
                    * of the same name exists, because the environment wins.
                    * Saying so beats wondering why a saved key had no effect. */
                   row.shadowed ? el("div", { class: "field__hint warn" },
                     "an environment variable of this name is set and takes "
                     + "precedence over anything saved here") : null),
                el("td", { class: "table__num" },
                  el("button", { class: "btn btn--sm", onclick: function () {
                    editSecret(row);
                  } }, row.state === "set" ? "Replace" : "Set"),
                  row.state === "set" ? el("button", { class: "btn btn--sm btn--quiet",
                    onclick: function () { clearSecret(row); } }, "Clear") : null));
            })))
        : el("div", { class: "card__body muted" }, "No keys are needed by the "
            + "profiles you have configured."));
  }

  function editSecret(row) {
    var input = el("input", { class: "input", type: "password",
                              placeholder: row.name, autocomplete: "off" });
    var handle = RF.dom.dialog(RF.dom.frag(
      el("div", { class: "dialog__head" },
        el("h2", { class: "card__title" },
           (row.state === "set" ? "Replace " : "Set ") + row.name)),
      el("div", { class: "dialog__body stack", "data-gap": "3" },
        input,
        el("div", { class: "field__hint" },
           "Written to data/secrets.json with owner-only permissions. It is never "
           + "returned by any endpoint — only whether it is set, and a masked form.")),
      el("div", { class: "dialog__foot" },
        el("button", { class: "btn btn--ghost", onclick: function () { handle.close(); } },
           "Cancel"),
        el("button", { class: "btn btn--primary", onclick: function () {
          var value = input.value.trim();
          if (!value) return;
          handle.close();
          RF.api.put("/api/settings/secrets", { name: row.name, value: value })
            .then(function () { RF.dom.toast(row.name + " saved", { kind: "ok" }); load(); })
            .catch(function (error) { RF.dom.toast(error.message, { kind: "error" }); });
        } }, "Save"))
    ), { label: "Set " + row.name });
    input.focus();
  }

  function clearSecret(row) {
    RF.dom.confirm({
      title: "Clear " + row.name + "?",
      body: "Anything using it stops working until it is set again.",
      confirmLabel: "Clear", danger: true,
    }).then(function (yes) {
      if (!yes) return;
      RF.api.del("/api/settings/secrets/" + encodeURIComponent(row.name))
        .then(function () { RF.dom.toast(row.name + " cleared", { kind: "ok" }); load(); })
        .catch(function (error) { RF.dom.toast(error.message, { kind: "error" }); });
    });
  }

  function load() {
    var host = document.querySelector("[data-settings]");
    return Promise.all([
      RF.api.get("/api/settings"),
      RF.api.get("/api/config/profiles"),
      RF.api.get("/api/settings/secrets").catch(function () { return { secrets: [] }; }),
    ]).then(function (results) {
      var settings = results[0] || {};
      var profiles = results[1] || {};
      var secrets = (results[2] || {}).secrets || [];
      // the profile editor reads `fields` off whichever adapter is chosen
      window.__RF_PROFILES = (settings.llm.profiles || [])
        .concat(settings.tts.profiles || [])
        .concat((settings.visuals || {}).profiles || []);
      var keyNames = secrets.map(function (row) { return row.name; });
      RF.dom.mount(host, [
        el("h1", { class: "section__title" }, "Settings"),
        el("datalist", { id: "known-keynames" }, keyNames.map(function (name) {
          return el("option", { value: name });
        })),
        profileList("llm", settings.llm || {}, settings.llm.adapters || []),
        profileList("tts", settings.tts || {}, settings.tts.adapters || []),
        profileList("visuals", settings.visuals || {}, (settings.visuals || {}).adapters || []),
        visualsCard(settings),
        rolesCard(settings),
        ttsKeyCard(settings),
        gatesCard(settings),
        secretsCard(secrets),
        (settings.access || {}).mode === "token" ? accessCard() : null,
      ].filter(Boolean));
    }).catch(function (error) {
      // 401 means the server wants the token and this browser has none, or a
      // stale one. That is recoverable from here, so offer the box rather
      // than an error -- the token is what would have let the page load.
      var body = error.status === 401
        ? [el("div", { class: "callout callout--info" },
              el("div", null,
                el("div", { class: "callout__title" }, "This ReelForge asks for an admin token"),
                el("div", null, RF.api.getToken()
                   ? "The token saved in this browser was not accepted. Paste "
                     + "the current one below."
                   : "It is published beyond its own machine, so the settings "
                     + "API -- which holds the API keys -- requires the token."))),
           accessCard(RF.api.getToken()
             ? "The saved token was rejected. Paste the value of "
               + "REELFORGE_ADMIN_TOKEN from the server."
             : null)]
        : error.status === 403
        ? [el("div", { class: "callout callout--info" },
              el("div", null,
                el("div", { class: "callout__title" }, "Settings are local-only"),
                el("div", null, "These controls answer only to a request from the " +
                   "machine ReelForge runs on, or one carrying the admin token. " +
                   "That is deliberate: they hold API keys.")))]
        : [el("div", { class: "callout callout--error" },
              el("div", null,
                el("div", { class: "callout__title" }, "Settings could not be loaded"),
                el("div", null, error.message)))];
      RF.dom.mount(host, [el("h1", { class: "section__title" }, "Settings")].concat(body));
    });
  }

  RF.shell.ready(function () { el = RF.dom.el; RF.stages.refresh(); load(); });
})(window.RF || (window.RF = {}));
