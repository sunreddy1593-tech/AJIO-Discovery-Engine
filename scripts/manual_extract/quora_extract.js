/**
 * Extract the visible Quora question plus each loaded answer into the Collect
 * JSON shape. Same contract as ajio_extract.js: an IIFE returning
 * `{ documents, warnings }`.
 *
 * Find threads with Google (`site:quora.com AJIO` + sizing / returns /
 * "worth buying"), open them yourself, scroll answers into view, then run this.
 * Quora's robots.txt forbids bots using the content for AI/ML, so this snippet
 * is the copy-paste, not a crawler.
 */
(function extractQuoraVisible() {
  var VIA =
    (typeof window !== "undefined" && window.__AJIO_EXTRACT_VIA__) ||
    "bookmarklet";
  var documents = [];
  var warnings = [];
  var seen = Object.create(null);
  var MIN_ANSWER_CHARS = 40;

  function textOf(node) {
    if (!node) return "";
    var clone = node.cloneNode(true);
    clone
      .querySelectorAll(
        "button, svg, script, style, noscript, [aria-label='More'], [class*='action']"
      )
      .forEach(function (el) {
        el.remove();
      });
    return (clone.innerText || clone.textContent || "").replace(/\s+/g, " ").trim();
  }

  function firstMatch(selectors) {
    for (var i = 0; i < selectors.length; i++) {
      var found = document.querySelectorAll(selectors[i]);
      if (found && found.length) return Array.prototype.slice.call(found);
    }
    return [];
  }

  function cleanBoilerplate(text) {
    return text
      .replace(/\b\d[\d.,k]*\s*(views?|upvotes?|answers?)\b/gi, " ")
      .replace(/\b(upvote|downvote|share|report|follow|continue reading|promoted)\b/gi, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  var url = (location.href || "").split("?")[0];
  var questionNode =
    document.querySelector("h1") ||
    document.querySelector("[class*='QuestionText']") ||
    document.querySelector("[class*='puppeteer_test_question_title']") ||
    document.querySelector("title");
  var question = cleanBoilerplate(textOf(questionNode));
  if (questionNode && questionNode.tagName === "TITLE") {
    question = question.replace(/\s*[-–|]\s*Quora.*$/i, "").trim();
  }
  if (!question) {
    warnings.push("could not find a question heading");
  }

  var answerNodes = firstMatch([
    ".q-box.qu-pt--medium .q-text",
    "[class*='puppeteer_test_answer_content']",
    "[class*='AnswerBase']",
    "div[class*='Answer'] .q-text",
    ".q-box .q-text.qu-display--block",
  ]);

  // Quora often mounts one .q-text per paragraph; prefer the nearest answer container.
  if (answerNodes.length > 8) {
    var containers = firstMatch([
      "[class*='AnswerBase']",
      ".q-box[class*='Answer']",
      "div[class*='spacing_log_answer_content']",
    ]);
    if (containers.length && containers.length < answerNodes.length) {
      answerNodes = containers;
    }
  }

  answerNodes.forEach(function (node, index) {
    var text = cleanBoilerplate(textOf(node));
    if (!text || text.length < MIN_ANSWER_CHARS) return;
    if (question && text === question) return;
    if (/^(sign in|continue with|related questions)$/i.test(text)) return;
    var key = text.toLowerCase();
    if (seen[key]) return;
    seen[key] = true;
    var authorNode = node.closest("div") &&
      (node.closest("[class*='Answer']") || node.parentElement);
    var authorEl = authorNode
      ? authorNode.querySelector("[class*='Name'], a[href*='/profile/'], [class*='author']")
      : null;
    documents.push({
      source: "quora_manual",
      url: url,
      text: text,
      author: authorEl ? textOf(authorEl) : null,
      timestamp: null,
      meta: {
        question: question || null,
        thread_title: question || null,
        answer_index: index,
        extraction: VIA,
      },
    });
  });

  var selection = typeof window.getSelection === "function" ? String(window.getSelection()) : "";
  selection = cleanBoilerplate(selection);
  if (selection && selection.length >= MIN_ANSWER_CHARS && documents.length === 0) {
    documents.push({
      source: "quora_manual",
      url: url,
      text: selection,
      author: null,
      timestamp: null,
      meta: {
        question: question || null,
        thread_title: question || null,
        extraction: VIA,
      },
    });
    warnings.push("fell back to the current text selection");
  }

  if (!documents.length) {
    warnings.push(
      "no answers visible. Scroll until answers load, expand 'Continue reading', then run again."
    );
  }

  var result = { documents: documents, warnings: warnings };
  if (typeof console !== "undefined" && console.log) {
    console.log("Quora extract:", documents.length, "answer(s)", warnings);
  }
  return result;
})();
