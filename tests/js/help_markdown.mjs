/* The Markdown renderer in help.js, run on the shapes the articles use.
 *
 * help.js is a browser script, so it is evaluated here with the globals it
 * closes over stubbed and the real `renderMarkdown` pulled out, the same way
 * calendar_logic.mjs does it. Run by `tests/test_help_js.py`. */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "app", "static", "help.js"), "utf8");
const { renderMarkdown } = new Function(`${src}\nreturn { renderMarkdown };`)();

const cases = [
  ["a heading and a paragraph joined across lines",
   "## How it works\n\nOne line\nand the next.",
   "<h2>How it works</h2>\n<p>One line and the next.</p>"],
  ["a list, with an indented line carrying on its item",
   "- one\n  still one\n- two",
   "<ul><li>one still one</li><li>two</li></ul>"],
  ["a numbered list",
   "1. first\n2. second",
   "<ol><li>first</li><li>second</li></ol>"],
  ["bold, italics and code; nothing inside code is formatted",
   "**bold** and *slanted* and `a **b** <c>`",
   "<p><b>bold</b> and <i>slanted</i> and <code>a **b** &lt;c&gt;</code></p>"],
  ["a link to another article jumps to it",
   "See [Calendar](06-calendar.md).",
   '<p>See <a href="#" data-help="06-calendar">Calendar</a>.</p>'],
  ["a web link opens in a new tab",
   "[docs](https://example.com/x)",
   '<p><a href="https://example.com/x" target="_blank" rel="noopener">docs</a></p>'],
  ["any other link is just its text",
   "[x](javascript:void)",
   "<p>x</p>"],
  ["HTML in an article is shown, never run",
   "<script>alert(1)</script>",
   "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"],
  ["a fenced block keeps its lines and is escaped",
   "```\na < b\n  indented\n```",
   "<pre><code>a &lt; b\n  indented</code></pre>"],
  ["a table skips its divider row",
   "| A | B |\n|---|---|\n| 1 | **2** |",
   '<div class="help-table"><table><thead><tr><th>A</th><th>B</th></tr></thead>' +
   "<tbody><tr><td>1</td><td><b>2</b></td></tr></tbody></table></div>"],
  ["a star inside a word is not italics",
   "2*3*4 stays",
   "<p>2*3*4 stays</p>"],
];

let failed = 0;
for (const [name, input, want] of cases) {
  const got = renderMarkdown(input);
  if (got !== want) {
    failed++;
    console.error(`FAIL  ${name}\n      got  ${JSON.stringify(got)}` +
                  `\n      want ${JSON.stringify(want)}`);
  }
}
if (failed) process.exit(1);
console.log(`ok  ${cases.length} cases`);
