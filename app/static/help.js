/* Help: one article per feature, saying how it works and why it was built
 * that way.
 *
 * The articles are Markdown files under `app/help/`, so they ship and change
 * with the code they describe, and read the same on GitHub as they do here.
 * `/api/help` hands over all of them at once; after that, moving between
 * articles never asks the server again.
 *
 * Like the calendar, it swaps the middle of the page and swaps it back. It
 * sits over whichever pane was showing without touching it, so closing Help
 * lands you exactly where you were.
 *
 * The renderer below covers the Markdown the articles actually use
 * (headings, paragraphs, lists, tables, fenced code, and inline code, bold,
 * italics and links) rather than all of Markdown. The app ships no
 * JavaScript dependencies, and the articles are ours to keep inside it. */

"use strict";

const help = { articles: null, open: false, current: null };

/* ---------------- Markdown ---------------- */

function helpEscape(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/* A link to another article is a relative `.md` link, which is what works on
 * GitHub; here it becomes a jump to that article. Web links open in a new
 * tab. Anything else is left as its text rather than guessed at. */
function helpInline(text) {
  const codes = [];
  let out = helpEscape(text).replace(/`([^`]+)`/g, (_, code) => {
    codes.push(`<code>${code}</code>`);
    return `\u0000${codes.length - 1}\u0000`;
  });
  out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, href) => {
    const article = href.match(/^([\w-]+)\.md$/);
    if (article) return `<a href="#" data-help="${article[1]}">${label}</a>`;
    if (/^https?:\/\//.test(href))
      return `<a href="${href}" target="_blank" rel="noopener">${label}</a>`;
    return label;
  });
  out = out.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?![*\w])/g, "$1<i>$2</i>");
  return out.replace(/\u0000(\d+)\u0000/g, (_, i) => codes[i]);
}

function renderMarkdown(source) {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const html = [];
  let para = [];
  let list = null;  // { tag, items: [] }

  const flushPara = () => {
    if (para.length) html.push(`<p>${helpInline(para.join(" "))}</p>`);
    para = [];
  };
  const flushList = () => {
    if (!list) return;
    const items = list.items.map((item) => `<li>${helpInline(item)}</li>`);
    html.push(`<${list.tag}>${items.join("")}</${list.tag}>`);
    list = null;
  };
  const flush = () => { flushPara(); flushList(); };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    if (/^```/.test(line)) {
      flush();
      const code = [];
      while (++i < lines.length && !/^```/.test(lines[i])) code.push(lines[i]);
      html.push(`<pre><code>${helpEscape(code.join("\n"))}</code></pre>`);
      continue;
    }

    const heading = line.match(/^(#{1,3})\s+(.*)$/);
    if (heading) {
      flush();
      const level = Math.max(2, heading[1].length);
      html.push(`<h${level}>${helpInline(heading[2])}</h${level}>`);
      continue;
    }

    if (/^\|/.test(line)) {
      flush();
      const rows = [];
      for (; i < lines.length && /^\|/.test(lines[i]); i++) rows.push(lines[i]);
      i--;
      const cells = (row) => row.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      // The second row is Markdown's divider, `|---|---|`: not content.
      const body = rows.slice(2).map((row) =>
        `<tr>${cells(row).map((c) => `<td>${helpInline(c)}</td>`).join("")}</tr>`);
      html.push(`<div class="help-table"><table><thead><tr>` +
        cells(rows[0]).map((c) => `<th>${helpInline(c)}</th>`).join("") +
        `</tr></thead><tbody>${body.join("")}</tbody></table></div>`);
      continue;
    }

    const item = line.match(/^(?:([-*])|\d+\.)\s+(.*)$/);
    if (item) {
      flushPara();
      const tag = item[1] ? "ul" : "ol";
      if (list && list.tag !== tag) flushList();
      if (!list) list = { tag, items: [] };
      list.items.push(item[2]);
      continue;
    }

    if (!line.trim()) { flush(); continue; }

    // An indented line carries on the list item above it.
    if (list && /^\s/.test(line)) {
      list.items[list.items.length - 1] += " " + line.trim();
      continue;
    }
    flushList();
    para.push(line.trim());
  }
  flush();
  return html.join("\n");
}

/* ---------------- the view ---------------- */

async function setHelpMode(on) {
  help.open = on;
  document.body.classList.toggle("help-mode", on);
  $("help-view").hidden = !on;
  $("btn-help").textContent = on ? "✕ Close help" : "❓";
  if (!on) return;
  if (!help.articles) {
    try {
      help.articles = await api("/help");
    } catch (e) {
      toast("Could not load help: " + e.message, true);
      setHelpMode(false);
      return;
    }
    renderHelpNav();
  }
  showHelpArticle(help.current || help.articles[0]?.slug);
}

function renderHelpNav() {
  const nav = $("help-nav");
  nav.replaceChildren(...help.articles.map((a) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "help-link";
    btn.dataset.slug = a.slug;
    btn.textContent = a.title;
    btn.addEventListener("click", () => showHelpArticle(a.slug));
    return btn;
  }));
}

function showHelpArticle(slug) {
  const article = help.articles.find((a) => a.slug === slug);
  if (!article) return;
  help.current = slug;
  $("help-article").innerHTML =
    `<h1>${helpEscape(article.title)}</h1>\n${renderMarkdown(article.body)}`;
  for (const btn of $("help-nav").children) {
    btn.classList.toggle("on", btn.dataset.slug === slug);
  }
  window.scrollTo(0, 0);
}

function wireHelp() {
  $("btn-help").addEventListener("click", () => setHelpMode(!help.open));
  // The calendar button means "show me the calendar" (or the list), which
  // Help would otherwise be sitting on top of.
  $("btn-view").addEventListener("click", () => { if (help.open) setHelpMode(false); });
  $("help-article").addEventListener("click", (e) => {
    const link = e.target.closest("a[data-help]");
    if (!link) return;
    e.preventDefault();
    showHelpArticle(link.dataset.help);
  });
}
