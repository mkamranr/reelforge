/* The API client.
 *
 * Three things this does that the v1 fetch wrapper does not, each because of a
 * shape the backend actually returns:
 *
 *  - FastAPI's `detail` is three different things: a string (400), a list of
 *    pydantic errors (422), and a dict (PUT /storyboard raises
 *    422 {"problems": [...]}). v1 shows users JSON.stringify of whichever it
 *    got. `humanise` collapses all three and keeps the raw value on the error.
 *  - Request groups. Switching stage twice quickly lets the slower response
 *    paint last. Each pane render takes a fresh group and aborts the previous.
 *  - SSE loss detection. EventSource reconnects for ever, including against a
 *    deleted job, so a dead stream looks exactly like a quiet one. After five
 *    consecutive errors the stream closes and says so.
 */
(function (RF) {
  "use strict";

  /* ---------------------------------------------------------------- token
   * The admin token, for a ReelForge published beyond localhost.
   *
   * `require_admin` checks the token before it checks the peer address, so
   * once REELFORGE_ADMIN_TOKEN is set on the server *every* client needs the
   * header -- including a browser on the machine itself. Without somewhere to
   * keep one, turning the token on simply broke the Settings page.
   *
   * It lives in localStorage and nowhere else: it is a credential for this
   * browser, not configuration, and it is never posted back to the server as
   * a setting.
   */
  var TOKEN_KEY = "reelforge.admin_token";

  function getToken() {
    try { return window.localStorage.getItem(TOKEN_KEY) || ""; } catch (err) { return ""; }
  }

  function setToken(value) {
    try {
      if (value) window.localStorage.setItem(TOKEN_KEY, value);
      else window.localStorage.removeItem(TOKEN_KEY);
    } catch (err) { /* private mode: the token just does not persist */ }
  }

  function authorise(headers) {
    var token = getToken();
    if (token) headers.authorization = "Bearer " + token;
    return headers;
  }

  function ApiError(status, detail, path) {
    var error = new Error(humanise(detail) || ("HTTP " + status));
    error.name = "ApiError";
    error.status = status;
    error.detail = detail;
    error.path = path;
    return error;
  }

  function humanise(detail) {
    if (!detail) return "";
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail.map(function (item) {
        if (typeof item === "string") return item;
        var where = (item.loc || []).filter(function (p) { return p !== "body"; }).join(".");
        return (where ? where + ": " : "") + (item.msg || JSON.stringify(item));
      }).join("; ");
    }
    if (detail.problems) return [].concat(detail.problems).join("\n");
    if (detail.message) return detail.message;
    return JSON.stringify(detail);
  }

  function request(path, options) {
    options = options || {};
    var init = {
      method: options.method || "GET",
      headers: authorise(Object.assign({}, options.headers)),
      signal: options.signal,
    };
    if (options.body !== undefined) {
      if (typeof options.body === "string") {
        init.headers["content-type"] = options.headers && options.headers["content-type"]
          ? options.headers["content-type"] : "text/plain";
        init.body = options.body;
      } else {
        init.headers["content-type"] = "application/json";
        init.body = JSON.stringify(options.body);
      }
    }
    return fetch(path, init).then(function (response) {
      var kind = response.headers.get("content-type") || "";
      var body = options.expect === "text" || (!kind.indexOf("text/") && options.expect !== "json")
        ? response.text() : response.json().catch(function () { return null; });
      return body.then(function (value) {
        if (!response.ok) {
          throw ApiError(response.status,
                         value && value.detail !== undefined ? value.detail : value, path);
        }
        return value;
      });
    });
  }

  /* Upload goes through XHR rather than fetch, because fetch still has no
   * upload progress and a 12 MB screenshot on a slow link needs one. */
  function upload(path, formData, options) {
    options = options || {};
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open("POST", path);
      // a separate code path from `request`, so it needs the header of its own
      var token = getToken();
      if (token) xhr.setRequestHeader("authorization", "Bearer " + token);
      if (options.onProgress && xhr.upload) {
        xhr.upload.onprogress = function (event) {
          if (event.lengthComputable) options.onProgress(event.loaded / event.total);
        };
      }
      xhr.onload = function () {
        var parsed = null;
        try { parsed = JSON.parse(xhr.responseText); } catch (err) { parsed = xhr.responseText; }
        if (xhr.status >= 200 && xhr.status < 300) return resolve(parsed);
        reject(ApiError(xhr.status, parsed && parsed.detail !== undefined ? parsed.detail : parsed,
                        path));
      };
      xhr.onerror = function () { reject(ApiError(0, "the upload could not be sent", path)); };
      xhr.send(formData);
    });
  }

  /* ------------------------------------------------------------------- SSE */
  /* No token here, and it cannot have one: EventSource has no headers API.
   * This works because /api/jobs is unguarded -- only /api/settings requires
   * the token. Anyone putting `require_admin` on the jobs router has to solve
   * live progress first, or the reel page goes quiet with no error. */
  function events(jobId, handlers) {
    handlers = handlers || {};
    var source = new EventSource("/api/jobs/" + encodeURIComponent(jobId) + "/events");
    var failures = 0;
    var closed = false;

    source.addEventListener("progress", function (message) {
      failures = 0;
      var payload;
      try { payload = JSON.parse(message.data); } catch (err) { return; }
      if (payload.type === "heartbeat") return handlers.onHeartbeat && handlers.onHeartbeat();
      if (payload.type === "stage") return handlers.onStage && handlers.onStage(payload);
      if (payload.type === "pipeline") return handlers.onPipeline && handlers.onPipeline(payload);
      if (handlers.onProgress) handlers.onProgress(payload);
    });
    source.onopen = function () { failures = 0; if (handlers.onOpen) handlers.onOpen(); };
    source.onerror = function () {
      failures += 1;
      if (failures >= 5 && !closed) {
        closed = true;
        source.close();
        if (handlers.onLost) handlers.onLost();
      }
    };
    return { close: function () { closed = true; source.close(); } };
  }

  /* Polling that stops when nobody is looking, and slows down when nothing is
   * changing. Opening an EventSource per job card would exhaust the six
   * connections a browser allows per origin. */
  function poll(fn, everyMs, options) {
    options = options || {};
    var timer = null, quiet = 0, stopped = false;

    function tick() {
      if (stopped) return;
      if (options.visibilityAware !== false && document.hidden) return schedule();
      Promise.resolve(fn()).then(function (changed) {
        quiet = changed === false ? quiet + 1 : 0;
      }).catch(function () {}).then(schedule);
    }
    function schedule() {
      if (stopped) return;
      var slow = options.backoff !== false && quiet >= 5;
      timer = setTimeout(tick, slow ? Math.min(everyMs * 4, 20000) : everyMs);
    }
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && !stopped) { clearTimeout(timer); tick(); }
    });
    tick();
    return { stop: function () { stopped = true; clearTimeout(timer); },
             now: function () { clearTimeout(timer); tick(); } };
  }

  function group() {
    var controller = new AbortController();
    return { signal: controller.signal, abort: function () { controller.abort(); } };
  }

  RF.api = {
    ApiError: ApiError,
    humanise: humanise,
    request: request,
    get: function (p, o) { return request(p, Object.assign({}, o)); },
    post: function (p, body, o) { return request(p, Object.assign({ method: "POST", body: body === undefined ? {} : body }, o)); },
    put: function (p, body, o) { return request(p, Object.assign({ method: "PUT", body: body }, o)); },
    patch: function (p, body, o) { return request(p, Object.assign({ method: "PATCH", body: body }, o)); },
    del: function (p, o) { return request(p, Object.assign({ method: "DELETE" }, o)); },
    text: function (p, o) { return request(p, Object.assign({ expect: "text" }, o)); },
    upload: upload, events: events, poll: poll, group: group,
    getToken: getToken, setToken: setToken,
  };
})(window.RF || (window.RF = {}));
