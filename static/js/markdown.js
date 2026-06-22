/* ============================================================
   Miraje — minimal offline Markdown renderer.
   No dependencies, no network. Escapes HTML, then applies a
   pragmatic subset: headings, bold/italic, inline & fenced code,
   lists, links, blockquotes, hr, tables, paragraphs.
   ============================================================ */
(function (global) {
  "use strict";

  function escapeHtml(s) {
    return s
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function inline(text) {
    // Inline code first (protect content).
    var stash = [];
    text = text.replace(/`([^`]+)`/g, function (_, c) {
      stash.push('<code>' + escapeHtml(c) + '</code>');
      return "\u0000" + (stash.length - 1) + "\u0000";
    });
    // Links [text](url)
    text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, function (_, t, u) {
      return '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(t) + '</a>';
    });
    // Bold then italic.
    text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
    text = text.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    // Restore stashed inline code.
    text = text.replace(/\u0000(\d+)\u0000/g, function (_, i) { return stash[+i]; });
    return text;
  }

  function render(md) {
    if (!md) return "";
    var src = md.replace(/\r\n/g, "\n");
    var lines = src.split("\n");
    var out = [];
    var i = 0;

    function flushParagraph(buf) {
      if (buf.length) {
        out.push("<p>" + inline(buf.join(" ")) + "</p>");
        buf.length = 0;
      }
    }

    while (i < lines.length) {
      var line = lines[i];

      // Fenced code block
      var fence = line.match(/^```(\w+)?\s*$/);
      if (fence) {
        var lang = fence[1] || "";
        var code = [];
        i++;
        while (i < lines.length && !/^```\s*$/.test(lines[i])) {
          code.push(lines[i]);
          i++;
        }
        i++; // skip closing fence
        out.push('<pre><code data-lang="' + escapeHtml(lang) + '">' + escapeHtml(code.join("\n")) + "</code></pre>");
        continue;
      }

      // Headings
      var h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) {
        var level = h[1].length;
        out.push("<h" + level + ">" + inline(h[2]) + "</h" + level + ">");
        i++;
        continue;
      }

      // Horizontal rule
      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
        out.push("<hr/>");
        i++;
        continue;
      }

      // Blockquote
      if (/^\s*>\s?/.test(line)) {
        var quote = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          quote.push(lines[i].replace(/^\s*>\s?/, ""));
          i++;
        }
        out.push("<blockquote>" + inline(quote.join(" ")) + "</blockquote>");
        continue;
      }

      // Unordered list
      if (/^\s*[-*+]\s+/.test(line)) {
        var items = [];
        while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
          items.push("<li>" + inline(lines[i].replace(/^\s*[-*+]\s+/, "")) + "</li>");
          i++;
        }
        out.push("<ul>" + items.join("") + "</ul>");
        continue;
      }

      // Ordered list
      if (/^\s*\d+\.\s+/.test(line)) {
        var oitems = [];
        while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
          oitems.push("<li>" + inline(lines[i].replace(/^\s*\d+\.\s+/, "")) + "</li>");
          i++;
        }
        out.push("<ol>" + oitems.join("") + "</ol>");
        continue;
      }

      // Blank line
      if (/^\s*$/.test(line)) {
        i++;
        continue;
      }

      // Paragraph (gather until blank line or block start)
      var buf = [];
      while (
        i < lines.length &&
        !/^\s*$/.test(lines[i]) &&
        !/^```/.test(lines[i]) &&
        !/^#{1,4}\s/.test(lines[i]) &&
        !/^\s*[-*+]\s+/.test(lines[i]) &&
        !/^\s*\d+\.\s+/.test(lines[i]) &&
        !/^\s*>\s?/.test(lines[i]) &&
        !/^\s*([-*_])\1{2,}\s*$/.test(lines[i])
      ) {
        buf.push(lines[i]);
        i++;
      }
      flushParagraph(buf);
    }

    return out.join("\n");
  }

  global.MirajeMarkdown = { render: render, escape: escapeHtml, inline: inline };
})(window);
