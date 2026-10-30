/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Agenda timeline: opens on the working hours, zoom buttons, a click on a free slot plans a to-do. */
(function () {
	"use strict";
	var tl = document.getElementById("timeline");
	if (!tl)
		return;
	var scroll = tl.querySelector(".tl-scroll");
	var from = +tl.dataset.from, to = +tl.dataset.to;
	var KEY = "agenda-zoom";
	var zoom = 1;
	try { zoom = +localStorage.getItem(KEY) || 1; } catch (e) { /* no storage */ }

	/* pixels per minute: the working hours fill the visible area at zoom 1 */
	function ppm() {
		var fit = scroll.clientHeight / Math.max(60, to - from + 30);
		return Math.min(6, Math.max(0.4, fit * zoom));
	}

	function apply(keepMinute) {
		var p = ppm();
		tl.style.setProperty("--ppm", p);
		scroll.scrollTop = Math.max(0, (keepMinute - 15) * p);
	}

	function topMinute() {
		var p = parseFloat(getComputedStyle(tl).getPropertyValue("--ppm")) || 1;
		return scroll.scrollTop / p + 15;
	}

	apply(from);

	tl.querySelectorAll("[data-zoom]").forEach(function (b) {
		b.addEventListener("click", function () {
			var keep = topMinute();
			zoom = Math.min(4, Math.max(0.25, zoom * (b.dataset.zoom > 0 ? 1.4 : 1 / 1.4)));
			try { localStorage.setItem(KEY, zoom); } catch (e) { /* no storage */ }
			apply(keep);
		});
	});

	if (!tl.dataset.plan)
		return;
	tl.querySelectorAll(".tl-col").forEach(function (col) {
		col.addEventListener("click", function (ev) {
			if (ev.target !== col)
				return;
			var p = parseFloat(getComputedStyle(tl).getPropertyValue("--ppm")) || 1;
			var m = Math.floor(ev.offsetY / p / 30) * 30;
			var hh = String(Math.floor(m / 60)).padStart(2, "0"), mm = String(m % 60).padStart(2, "0");
			location.href = tl.dataset.plan + "?plan_date=" + col.dataset.day + "&plan_time=" + hh + ":" + mm +
				"&next=" + encodeURIComponent(tl.dataset.back);
		});
	});
})();
