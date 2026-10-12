"""QA answers are read the way CommonMark shows them, and a fence can never vouch for a PASS.

Ported from the PhonicsMaker loop, where four QA rounds each found a Markdown
corner a home-made fence reader got wrong. The reader stays zero-dependency;
when markdown-it-py is installed, a randomized check compares it line by line
with a reference CommonMark parser and fails on any line Sayso would read as
plain that CommonMark hides.
"""
from __future__ import annotations

import itertools
import random
import unittest

from sayso.jobs import qa

H = "8aa1bf0f629fa774e4b8cd292d39b3a222dca757"
PASS = f"VERDICT: PASS {H}"


def ends_plain(text):
    return qa._ends_on_plain_line(text)


class FenceRules(unittest.TestCase):
    CASES = [
        (f"ok\n{PASS}\n", True),
        (f"```\nx\n```\n{PASS}\n", True),
        (f"```\nx\n    ```\n{PASS}\n", False),           # an indented closer does not close
        (f"````\nx\n```\n{PASS}\n", False),              # a shorter closer does not close
        (f"~~~\nx\n```\n{PASS}\n", False),               # another character does not close
        (f"```\nx\n```  trailing\n{PASS}\n", False),     # a closer with text does not close
        (f"    ```\n{PASS}\n", True),                    # 4 spaces: not a fence
        (f"    ~~~~\n{PASS}\n", True),                   # 4 spaces: not a fence, either character
        (f"x\n     ```\n{PASS}\n", True),                # inside a paragraph, 5 spaces: still text
        (f"    code\n<span>\n{PASS}\n", False),          # after indented code an inline tag opens an HTML block
        (f"\t```\n{PASS}\n", True),                      # a tab is 4 spaces: not a fence
        (f"   ```\n{PASS}\n", False),                    # 3 spaces: a fence
        (f"``` a`b\n{PASS}\n", True),                    # backtick info with a backtick: not a fence
        (f"> quoted\n{PASS}\n", True),                   # lazy continuation is not hidden
        (f"> {PASS}\n", False),
        (f"<!--\n{PASS}\n", False),                      # unclosed HTML comment
        (f"<div>\n{PASS}\n", False),                     # HTML block runs to a blank line
        (f"<div>\n\n{PASS}\n", True),
        (f"- ```\n{PASS}\n", True),                      # a fence in a list item ends with the item
        (f"- ```\n  {PASS}\n", False),
        (f"ok\r\n{PASS}\r\n", True),                     # CRLF
    ]

    def test_the_last_line_is_plain_exactly_as_commonmark_says(self):
        for text, plain in self.CASES:
            with self.subTest(text=text):
                self.assertIs(ends_plain(text), plain)
                self.assertEqual(qa.parse_verdict(text, H)["verdict"], "PASS" if plain else "BLOCKED")

    def test_an_earlier_plain_pass_never_vouches_for_a_fenced_final_one(self):
        text = f"{PASS}\n```\n{PASS}\n"
        self.assertEqual(qa.parse_verdict(text, H)["verdict"], "BLOCKED")

    def test_a_blocked_inside_a_fence_or_quote_still_wins(self):
        for wrap in ("```\n{}\n```", "> {}", "<!--\n{}\n-->"):
            text = wrap.format(f"VERDICT: BLOCKED {H} broke") + f"\n{PASS}"
            self.assertEqual(qa.parse_verdict(text, H)["verdict"], "BLOCKED")

    def test_every_fence_shape(self):
        for ch, n, indent, closer in itertools.product("`~", (3, 4, 7), ("", " ", "   ", "    ", "\t"),
                                                       ("", "```", "~~~~", "    ```", "\t~~~", "`` x", "~~~~~~~")):
            text = f"{indent}{ch * n}\n{closer}\n{PASS}\n"
            opens = len(indent.expandtabs(4)) <= 3
            closes = (closer.strip() == closer and len(closer) >= n and set(closer) == {ch})
            closer_opens = closer in ("```", "~~~~", "~~~~~~~")    # a fence of its own when line 1 is not one
            with self.subTest(text=text):
                self.assertIs(ends_plain(text), closes if opens else not closer_opens)

    def test_hidden_labels_do_not_count(self):
        for start, end in [("```\n", "```\n"), ("~~~~\n", "~~~~\n"), ("> ", ""), ("> > ", ""), ("  > ", ""),
                           ("<!--\n", "-->\n"), ("<pre>\n", "</pre>\n"), ("<script>\n", "</script>\n"),
                           ("<div>\n", "\n"), ("- ```\n  ", "  ```\n"), ("> ```\n> ", "> ```\n")]:
            for line in ("BLOCK-KIND: EVIDENCE screenshot proof", "The QA tool timed out.",
                         "CUSTOMER-FIX: YES checked"):
                text = start + line + "\n" + end + f"VERDICT: BLOCKED {H} missing proof\n"
                with self.subTest(text=text):
                    self.assertNotIn(line, qa._plain_lines(text))
                    self.assertNotEqual(qa.block_kind(text)[0], "EVIDENCE")
                    self.assertFalse(qa.tool_timeout_block(text))
                    self.assertNotEqual(qa.customer_fix_problem(text), "")


try:
    from markdown_it import MarkdownIt
except ImportError:          # Sayso has no dependencies; the cross-check runs where it is installed
    MarkdownIt = None


@unittest.skipIf(MarkdownIt is None, "markdown-it-py not installed (pip install markdown-it-py to cross-check)")
class AgainstReferenceCommonMark(unittest.TestCase):
    """Sayso never reads as plain a line a reference CommonMark parser hides."""

    ATOMS = ["```", "~~~", "````", "~~~~", " ```", "   ```", "    ```", "\t```", "```py", "``` a`b",
             ">", "> ", ">>", "   >", "    >", "-", "- ", "* ", "1. ", "2) ", "  - ", "   1. ", "\t- ",
             "<!--", "-->", "<pre>", "</pre>", "<script>", "<div>", "</div>", "<span>", '<a href="x">', "<?", "?>",
             "<!X", "<![CDATA[", "]]>", "", "x", PASS, "BLOCK-KIND: EVIDENCE p", "***", "---", "===", "# h",
             "    code", "  text"]

    def ref_mask(self, text):
        lines = qa._md_lines(text)
        plain = [True] * len(lines)
        for t in MarkdownIt("commonmark").parse("\n".join(lines)):
            if t.type in ("fence", "html_block") and t.map:
                for i in range(t.map[0], min(t.map[1], len(lines))):
                    plain[i] = False
            elif t.type == "blockquote_open" and t.map:
                for i in range(t.map[0], min(t.map[1], len(lines))):
                    if lines[i].lstrip().startswith(">"):
                        plain[i] = False
        return plain

    def test_randomized_answers(self):
        rnd = random.Random(20261012)
        for _ in range(20000):
            lines = ["".join(rnd.choice(self.ATOMS) for _ in range(rnd.randint(1, 3)))
                     for _ in range(rnd.randint(1, 8))]
            text = "\n".join(lines)
            ours, ref = qa._plain_mask(text), self.ref_mask(text)
            unsafe = [i for i, (a, b) in enumerate(zip(ours, ref)) if a and not b]
            self.assertEqual(unsafe, [], f"plain in Sayso but hidden in CommonMark: {lines!r}")


if __name__ == "__main__":
    unittest.main()
