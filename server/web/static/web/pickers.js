/* SPDX-License-Identifier: GPL-3.0-or-later */
/*
 * Date and time pickers (issue #38). The browser's own inputs follow its
 * language, often the American format; here each becomes a text field in
 * the user's formats (body data-date: eu, us, iso; data-time: 24, 12) that
 * can be typed in, with a calendar and the app's grid of 5-minute cells
 * (TimeGrid.kt): one row per hour, 12 hours at a time, ▲ Prima / Dopo ▼
 * move by 3 hours. The start and end of a form are chosen together:
 * dragging from the first cell to the last, or tapping the first and then
 * the last; the cells of the other segments of that day are hatched.
 * The original input stays, hidden, with its ISO value: the server reads
 * the forms as before.
 */
(function () {
	"use strict";
	var body = document.body;
	var DF = body.dataset.date || "eu", TF = body.dataset.time || "24";
	var T = {};
	try {
		T = JSON.parse(body.dataset.pickerTexts) || {};
	} catch (e) { /* the texts stay the keys */ }
	var STEP = 5, CELLS = 1440 / STEP, HOURS = 12;
	var lang = document.documentElement.lang || "it";

	function pad(n) {
		return (n < 10 ? "0" : "") + n;
	}
	function t(key) {
		return T[key] || key;
	}

	// ---- formats ----

	function isoDate(d) {
		return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
	}
	function dateOf(iso) {
		var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
		return m ? new Date(+m[1], m[2] - 1, +m[3]) : null;
	}
	function showDate(iso) {
		var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
		if (!m)
			return "";
		if (DF === "us")
			return m[2] + "/" + m[3] + "/" + m[1];
		if (DF === "iso")
			return m[1] + "-" + m[2] + "-" + m[3];
		return m[3] + "/" + m[2] + "/" + m[1];
	}
	function showTime(min) {
		if (min === null || min === undefined)
			return "";
		var h = Math.floor(min / 60) % 24, m = min % 60;
		if (TF === "12")
			return ((h + 11) % 12 + 1) + ":" + pad(m) + " " + (h < 12 ? "AM" : "PM");
		return pad(h) + ":" + pad(m);
	}
	function hourLabel(h) {
		return TF === "12" ? ((h + 11) % 12 + 1) + (h < 12 ? " AM" : " PM") : pad(h);
	}
	function isoTime(min) {
		return pad(Math.floor(min / 60) % 24) + ":" + pad(min % 60);
	}
	function minuteOf(hhmm) {
		var m = /(\d{2}):(\d{2})/.exec(hhmm || "");
		return m ? +m[1] * 60 + +m[2] : null;
	}
	function duration(min) {
		return Math.floor(min / 60) + ":" + pad(min % 60);
	}
	// typed text: "" when empty, null when not valid
	function parseDate(text) {
		var p = text.trim().split(/[^0-9]+/).filter(Boolean);
		if (!p.length)
			return "";
		if (p.length !== 3)
			return null;
		var y, mo, d;
		if (p[0].length === 4) {
			y = p[0]; mo = p[1]; d = p[2];
		} else if (DF === "us") {
			mo = p[0]; d = p[1]; y = p[2];
		} else {
			d = p[0]; mo = p[1]; y = p[2];
		}
		y = +y < 100 ? +y + 2000 : +y;
		var dt = new Date(y, mo - 1, +d);
		if (dt.getFullYear() !== y || dt.getMonth() !== mo - 1 || dt.getDate() !== +d)
			return null;
		return isoDate(dt);
	}
	function parseTime(text) {
		var s = text.trim().toLowerCase();
		if (!s)
			return "";
		var pm = /p\.?(m\.?)?$/.test(s), am = /a\.?(m\.?)?$/.test(s);
		s = s.replace(/[ap]\.?(m\.?)?$/, "").trim();
		var m = /^(\d{1,2})(?:[:.h ]?(\d{2}))?$/.exec(s);
		if (!m)
			return null;
		var h = +m[1], mi = m[2] ? +m[2] : 0;
		if (am || pm) {
			if (h < 1 || h > 12)
				return null;
			h = h % 12 + (pm ? 12 : 0);
		}
		return h > 23 || mi > 59 ? null : h * 60 + mi;
	}
	function placeholder(kind) {
		var d = t("fmt_" + DF), tm = t(TF === "12" ? "fmt_t12" : "fmt_t24");
		return kind === "date" ? d : kind === "time" ? tm : d + " " + tm;
	}

	// ---- the fields ----

	function Field(src) {
		this.src = src;
		this.kind = src.type === "datetime-local" ? "datetime" : src.type;
		this.wrap = document.createElement("span");
		this.wrap.className = "picker";
		this.text = document.createElement("input");
		this.text.type = "text";
		this.text.autocomplete = "off";
		this.text.placeholder = src.placeholder || placeholder(this.kind);
		this.text.required = src.required;
		this.text.setAttribute("aria-label", src.closest("label") ? src.closest("label").firstChild.textContent.trim() : src.name);
		this.button = document.createElement("button");
		this.button.type = "button";
		this.button.className = "icon-button";
		this.button.title = t(this.kind === "time" ? "time" : "date");
		this.button.innerHTML = '<svg class="i" aria-hidden="true"><use href="#i-agenda"/></svg>';
		src.parentNode.insertBefore(this.wrap, src);
		this.wrap.appendChild(this.text);
		this.wrap.appendChild(this.button);
		this.wrap.appendChild(src);
		src.required = false;
		src.type = "hidden";
		src.picker = this;
		this.show();
		var self = this;
		this.text.addEventListener("change", function () { self.typed(); });
		this.button.addEventListener("click", function () { open(self); });
		this.text.addEventListener("keydown", function (ev) {
			if (ev.key === "ArrowDown" && ev.altKey)
				open(self);
		});
	}
	Field.prototype.date = function () {
		return this.kind === "time" ? "" : (this.src.value || "").slice(0, 10);
	};
	Field.prototype.minute = function () {
		return this.kind === "date" ? null : minuteOf(this.src.value);
	};
	Field.prototype.show = function () {
		var parts = [];
		if (this.kind !== "time")
			parts.push(showDate(this.date()));
		if (this.kind !== "date")
			parts.push(showTime(this.minute()));
		this.text.value = parts.filter(Boolean).join(" ");
		this.text.setCustomValidity("");
	};
	// a date ("" none) and minutes of the day (1440: the next day's midnight)
	Field.prototype.set = function (day, min) {
		var v = "";
		if (this.kind === "date")
			v = day;
		else if (this.kind === "time")
			v = min === null ? "" : isoTime(min);
		else if (day && min !== null) {
			var d = dateOf(day);
			if (min >= 1440) {
				d.setDate(d.getDate() + 1);
				min -= 1440;
			}
			v = isoDate(d) + "T" + isoTime(min);
		}
		this.src.value = v;
		this.show();
		this.src.dispatchEvent(new Event("change", { bubbles: true }));
	};
	Field.prototype.typed = function () {
		var text = this.text.value.trim(), day = "", min = null;
		if (this.kind === "date") {
			day = parseDate(text);
		} else if (this.kind === "time") {
			min = parseTime(text);
			if (min === "")
				min = null;
			else if (min === null)
				day = null;
		} else if (text) {
			var cut = text.search(/\s/);
			day = cut < 0 ? null : parseDate(text.slice(0, cut));
			min = cut < 0 ? null : parseTime(text.slice(cut + 1));
			if (!day || min === null || min === "")
				day = null;
		}
		if (day === null) {
			this.text.setCustomValidity(t("invalid"));
			this.text.reportValidity();
			return;
		}
		this.set(day, min);
	};
	// the start and end of the same form, both shown: chosen together
	Field.prototype.pair = function () {
		var name = this.src.name, form = this.src.form;
		if (!form || (name !== "start" && name !== "end"))
			return null;
		var s = form.elements.start, e = form.elements.end;
		if (!s || !e || !s.picker || !e.picker || s.picker.kind !== e.picker.kind)
			return null;
		if (!s.picker.wrap.offsetParent || !e.picker.wrap.offsetParent)
			return null;
		return { start: s.picker, end: e.picker };
	};

	// ---- the popup ----

	var pop = null, state = null, busyCache = {};

	function close() {
		if (pop) {
			pop.remove();
			pop = null;
			state = null;
		}
	}
	document.addEventListener("pointerdown", function (ev) {
		if (pop && !pop.contains(ev.target) && !(state && state.field.wrap.contains(ev.target)))
			close();
	});
	document.addEventListener("keydown", function (ev) {
		if (ev.key === "Escape" && pop) {
			var f = state.field;
			close();
			f.text.focus();
		}
	});

	function el(tag, cls, text) {
		var e = document.createElement(tag);
		if (cls)
			e.className = cls;
		if (text !== undefined)
			e.textContent = text;
		return e;
	}
	function button(text, cls, fn) {
		var b = el("button", cls, text);
		b.type = "button";
		b.addEventListener("click", fn);
		return b;
	}

	function open(field) {
		if (state && state.field === field) {
			close();
			return;
		}
		close();
		var pair = field.kind === "date" ? null : field.pair();
		var first = pair ? pair.start : field;
		var day = first.date() || field.date() || isoDate(new Date());
		var sMin = pair ? pair.start.minute() : field.minute();
		var eMin = pair ? pair.end.minute() : null;
		if (pair && eMin !== null && pair.end.date() && pair.end.date() !== day)
			eMin += 1440;
		var at = field.minute() !== null ? field.minute() : sMin;
		state = {
			field: field, pair: pair, day: day, month: dateOf(day),
			first: at !== null ? Math.max(0, Math.min(24 - HOURS, Math.floor(at / 60) - 2)) : 7,
			// the chosen cells: a, b (indices); range mode waits for the second
			a: null, b: null, waiting: false, dragging: false,
			shown: { s: sMin, e: eMin }, busy: []
		};
		pop = el("div", "pk-pop");
		pop.setAttribute("role", "dialog");
		field.wrap.appendChild(pop);
		render();
		var r = pop.getBoundingClientRect();
		if (r.right > window.innerWidth - 8)
			pop.classList.add("pk-right");
		pop.scrollIntoView({ block: "nearest" });
		loadBusy();
	}

	function render() {
		var f = state.field;
		pop.textContent = "";
		var panes = el("div", "pk-panes");
		if (f.kind !== "time")
			panes.appendChild(calendar());
		if (f.kind !== "date")
			panes.appendChild(grid());
		pop.appendChild(panes);
		pop.appendChild(footer());
		if (f.kind !== "date")
			paint();
	}

	function calendar() {
		var box = el("div", "pk-cal");
		var month = state.month;
		var head = el("div", "pk-head");
		head.appendChild(button("‹", "icon-button", function () { state.month = new Date(month.getFullYear(), month.getMonth() - 1, 1); render(); }));
		head.appendChild(el("b", "", new Intl.DateTimeFormat(lang, { month: "long", year: "numeric" }).format(month)));
		head.appendChild(button("›", "icon-button", function () { state.month = new Date(month.getFullYear(), month.getMonth() + 1, 1); render(); }));
		box.appendChild(head);
		var days = el("div", "pk-days");
		var wd = new Intl.DateTimeFormat(lang, { weekday: "narrow" });
		for (var i = 0; i < 7; i++)
			days.appendChild(el("span", "pk-wd", wd.format(new Date(2024, 0, 1 + i))));
		var start = new Date(month.getFullYear(), month.getMonth(), 1);
		start.setDate(1 - (start.getDay() + 6) % 7);
		var today = isoDate(new Date());
		for (var k = 0; k < 42; k++) {
			var d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + k);
			var iso = isoDate(d);
			var b = button(String(d.getDate()), "pk-day", pickDay.bind(null, iso));
			if (d.getMonth() !== month.getMonth())
				b.classList.add("out");
			if (iso === today)
				b.classList.add("today");
			if (iso === state.day && (state.field.date() || state.field.kind !== "date"))
				b.classList.add("on");
			days.appendChild(b);
		}
		box.appendChild(days);
		return box;
	}

	function pickDay(iso) {
		state.day = iso;
		if (state.field.kind === "date") {
			state.field.set(iso, null);
			var f = state.field;
			close();
			f.text.focus();
			return;
		}
		state.month = dateOf(iso);
		render();
		loadBusy();
	}

	// the selection, minutes [from, to) of the day
	function selection() {
		if (state.a !== null) {
			var lo = Math.min(state.a, state.b), hi = Math.max(state.a, state.b);
			return { s: lo * STEP, e: state.waiting ? null : (hi + 1) * STEP };
		}
		return state.shown;
	}

	function grid() {
		var box = el("div", "pk-grid");
		var head = el("div", "pk-head");
		head.appendChild(el("b", "pk-sel"));
		head.appendChild(el("span", "doc pk-hint", t("pick_end")));
		box.appendChild(head);
		box.appendChild(button(t("before"), "pk-move", function () { state.first = Math.max(0, state.first - 3); render(); }));
		var rows = el("div", "pk-rows");
		rows.style.setProperty("--label", TF === "12" ? "46px" : "28px");
		for (var h = state.first; h < state.first + HOURS; h++) {
			var row = el("div", "pk-row");
			row.appendChild(el("span", "pk-hour", hourLabel(h)));
			for (var c = 0; c < 12; c++) {
				var i = h * 12 + c, m = i * STEP;
				var cell = el("span", "pk-cell");
				cell.dataset.i = i;
				cell.title = showTime(m);
				if (state.busy.some(function (r) { return m < r[1] && m + STEP > r[0]; }))
					cell.classList.add("busy");
				row.appendChild(cell);
			}
			rows.appendChild(row);
		}
		rows.addEventListener("pointerdown", down);
		rows.addEventListener("pointermove", move);
		rows.addEventListener("pointerup", up);
		box.appendChild(rows);
		box.appendChild(button(t("after"), "pk-move", function () { state.first = Math.min(24 - HOURS, state.first + 3); render(); }));
		return box;
	}

	// the chosen cells and the text above them, without rebuilding the grid (a drag goes on)
	function paint() {
		var sel = selection();
		pop.querySelectorAll(".pk-cell").forEach(function (cell) {
			var m = +cell.dataset.i * STEP;
			cell.classList.toggle("on", sel.s !== null && (sel.e !== null ? m >= sel.s && m < sel.e : m === sel.s - sel.s % STEP));
		});
		var text = showTime(sel.s);
		if (state.pair) {
			text = (sel.s !== null ? showTime(sel.s) : "…") + " – " + (sel.e !== null ? showTime(sel.e) : "…");
			if (sel.s !== null && sel.e !== null && sel.e > sel.s)
				text += " · " + duration(sel.e - sel.s);
		}
		var head = pop.querySelector(".pk-sel");
		if (head)
			head.textContent = text || "—";
		var hint = pop.querySelector(".pk-hint");
		if (hint)
			hint.hidden = !(state.pair && state.waiting);
	}

	function cellAt(ev) {
		var e = document.elementFromPoint(ev.clientX, ev.clientY);
		return e && e.classList.contains("pk-cell") ? +e.dataset.i : null;
	}
	function down(ev) {
		var i = cellAt(ev);
		if (i === null)
			return;
		ev.preventDefault();
		if (state.pair && state.waiting) {
			state.b = i;
			state.waiting = false;
		} else {
			state.a = state.b = i;
			state.waiting = true;
			state.dragging = true;
		}
		paint();
	}
	function move(ev) {
		if (!state.dragging)
			return;
		var i = cellAt(ev);
		if (i !== null && i !== state.b) {
			state.b = i;
			paint();
		}
	}
	function up(ev) {
		if (!state.dragging)
			return;
		state.dragging = false;
		if (!state.pair) {
			apply();
			return;
		}
		var i = cellAt(ev);
		if (i !== null)
			state.b = i;
		if (state.b !== state.a)
			state.waiting = false;
		paint();
	}

	// what the grid chose goes to the fields
	function apply() {
		var f = state.field, day = state.day;
		if (state.a !== null) {
			var lo = Math.min(state.a, state.b), hi = Math.max(state.a, state.b);
			if (!state.pair)
				f.set(day, lo * STEP);
			else if (state.waiting) {
				// one cell only: the field opened takes it, the other stays
				if (f === state.pair.start)
					f.set(day, lo * STEP);
				else
					f.set(day, (lo + 1) * STEP);
			} else {
				state.pair.start.set(day, lo * STEP);
				state.pair.end.set(day, (hi + 1) * STEP);
			}
		} else if (f.kind === "datetime" && f.minute() !== null && day !== f.date()) {
			// only the day changed: the times move with it
			if (state.pair) {
				state.pair.start.set(day, state.pair.start.minute());
				if (state.pair.end.minute() !== null)
					state.pair.end.set(day, state.shown.e !== null ? state.shown.e : state.pair.end.minute());
			} else {
				f.set(day, f.minute());
			}
		}
		close();
		f.text.focus();
	}

	function footer() {
		var f = state.field;
		var bar = el("div", "pk-foot");
		if (f.kind !== "date") {
			var exact = el("input", "pk-exact");
			exact.type = "text";
			exact.placeholder = t(TF === "12" ? "fmt_t12" : "fmt_t24");
			exact.setAttribute("aria-label", t("exact"));
			exact.addEventListener("keydown", function (ev) {
				if (ev.key !== "Enter")
					return;
				ev.preventDefault();
				var m = parseTime(exact.value);
				if (m === null || m === "") {
					exact.setCustomValidity(t("invalid"));
					exact.reportValidity();
					return;
				}
				f.set(state.day, m);
				close();
				f.text.focus();
			});
			var lab = el("label", "pk-exact-l");
			lab.appendChild(document.createTextNode(t("exact")));
			lab.appendChild(exact);
			bar.appendChild(lab);
		} else {
			bar.appendChild(button(t("today"), "", function () { pickDay(isoDate(new Date())); }));
		}
		var right = el("span", "pk-buttons");
		if (!f.text.required)
			right.appendChild(button(t("clear"), "", function () { f.set("", null); close(); f.text.focus(); }));
		right.appendChild(button(t("cancel"), "", function () { close(); f.text.focus(); }));
		if (f.kind !== "date")
			right.appendChild(button(t("ok"), "primary", apply));
		bar.appendChild(right);
		return bar;
	}

	function loadBusy() {
		var f = state.field, url = body.dataset.busy;
		if (!url || f.kind === "date")
			return;
		var form = f.src.form, q = "?day=" + state.day;
		if (form && form.dataset.entry)
			q += "&entry=" + encodeURIComponent(form.dataset.entry);
		if (form && form.elements.segment)
			q += "&segment=" + encodeURIComponent(form.elements.segment.value);
		var mine = state;
		var done = function (busy) {
			if (state === mine) {
				state.busy = busy;
				render();
			}
		};
		if (busyCache[q]) {
			done(busyCache[q]);
			return;
		}
		fetch(url + q, { credentials: "same-origin" }).then(function (r) { return r.ok ? r.json() : { busy: [] }; })
			.then(function (j) { busyCache[q] = j.busy || []; done(busyCache[q]); })
			.catch(function () { /* no hatching */ });
	}

	document.querySelectorAll('input[type=date], input[type=time], input[type=datetime-local]').forEach(function (src) {
		new Field(src);
	});
})();
