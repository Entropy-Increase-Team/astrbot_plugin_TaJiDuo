import asyncio
import base64
import mimetypes
import os
import re
import time
import uuid
from typing import Any, Dict, Optional

import jinja2
from astrbot.api import logger


class Renderer:
    _jinja_env: Optional[jinja2.Environment] = None

    @classmethod
    def _get_jinja_env(cls) -> jinja2.Environment:
        if cls._jinja_env is None:
            cls._jinja_env = jinja2.Environment(
                autoescape=True,
                keep_trailing_newline=True,
            )
        return cls._jinja_env

    def __init__(self, res_path: str, render_timeout: int = 30000) -> None:
        self.res_path = res_path
        self.render_timeout = render_timeout
        self._browser = None
        self._playwright = None
        self._lock = asyncio.Lock()
        self._output_dir = os.path.abspath(os.path.join(self.res_path, "render_cache"))
        os.makedirs(self._output_dir, exist_ok=True)
        self._cleanup_task: Optional[asyncio.Task] = asyncio.create_task(
            self._cache_cleanup_loop()
        )

    def get_res_path(self, sub_path: str) -> str:
        return "file:///" + os.path.abspath(os.path.join(self.res_path, sub_path)).replace(
            "\\", "/"
        )

    async def _cache_cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                now = time.time()
                for name in os.listdir(self._output_dir):
                    if not name.startswith("render_"):
                        continue
                    full_path = os.path.join(self._output_dir, name)
                    try:
                        age = now - os.path.getmtime(full_path)
                    except OSError:
                        continue
                    if age > 300:
                        try:
                            os.remove(full_path)
                        except OSError:
                            pass
            except asyncio.CancelledError:
                break
            except Exception:
                continue

    def get_template(self, name: str) -> str:
        file_path = os.path.join(self.res_path, name)
        if not os.path.exists(file_path):
            return ""
        with open(file_path, "r", encoding="utf-8") as file:
            return file.read()

    def _adapt_template(self, content: str) -> str:
        adapted = content.replace("$index+1", "loop.index").replace(
            "$index", "loop.index0"
        )
        adapted = adapted.replace("$value", "item")

        def _fix_condition(match: re.Match[str]) -> str:
            condition = (
                match.group(1)
                .replace("===", "==")
                .replace("!==", "!=")
                .replace("&&", " and ")
                .replace("||", " or ")
                .replace("null", "none")
                .replace(".length", "|length")
            )
            condition = re.sub(r"!\s*([\w\.]+)", r"not \1", condition)
            return f"{{% if {condition} %}}"

        adapted = re.sub(r"\{\{if\s+(.+?)\}\}", _fix_condition, adapted)
        adapted = adapted.replace("{{/if}}", "{% endif %}").replace(
            "{{else}}", "{% else %}"
        )
        adapted = re.sub(
            r"\{\{else if\s+(.+?)\}\}",
            lambda m: _fix_condition(m).replace("{% if", "{% elif"),
            adapted,
        )

        def _replace_each(match: re.Match[str]) -> str:
            inner = match.group(1).strip().split()
            if len(inner) >= 2:
                return f"{{% for {inner[1]} in {inner[0]} %}}"
            return f"{{% for item in {inner[0]} %}}"

        adapted = re.sub(r"\{\{\s*each\s+(.+?)\s*\}\}", _replace_each, adapted)
        adapted = adapted.replace("{{/each}}", "{% endfor %}")
        adapted = re.sub(
            r"\{\{@\s*(.+?)\s*\}\}",
            lambda m: (
                "{{"
                + m.group(1)
                .split("||")[0]
                .replace("&&", " and ")
                .replace("null", "none")
                .replace(".length", "|length")
                + "|safe}}"
            ),
            adapted,
        )
        adapted = re.sub(
            r"\{\{([^%\}]+?)\}\}",
            lambda m: (
                "{{"
                + m.group(1)
                .split("||")[0]
                .replace("&&", " and ")
                .replace("null", "none")
                .replace(".length", "|length")
                + "}}"
            ),
            adapted,
        )
        return adapted

    def _inline_assets(self, html: str) -> str:
        def _inline_css(match: re.Match[str]) -> str:
            path = os.path.join(self.res_path, match.group(1))
            if not os.path.exists(path):
                return ""
            with open(path, "r", encoding="utf-8", errors="replace") as file:
                css_content = file.read()
            css_content = self._adapt_template(css_content)
            return f"<style>\n{css_content}\n</style>"

        def _inline_image(match: re.Match[str]) -> str:
            path = os.path.join(self.res_path, match.group(1))
            if not os.path.exists(path):
                return match.group(0)
            mime = mimetypes.guess_type(path)[0] or "image/png"
            with open(path, "rb") as file:
                b64 = base64.b64encode(file.read()).decode("utf-8")
            if match.group(0).startswith("src"):
                return f'src="data:{mime};base64,{b64}"'
            return f"url(data:{mime};base64,{b64})"

        html = re.sub(
            r'<link\s+rel="stylesheet"\s+href="\{\{(?:_res_path|pluResPath)\}\}([^"]+\.css)">',
            _inline_css,
            html,
        )
        html = re.sub(
            r'src="\{\{(?:_res_path|pluResPath)\}\}([^"]+\.(?:png|jpg|jpeg|gif|svg|webp))"',
            _inline_image,
            html,
        )
        html = re.sub(
            r'url\(\s*[\'"]?\{\{(?:_res_path|pluResPath)\}\}([^)"\']+?)[\'"]?\s*\)',
            _inline_image,
            html,
        )
        return html

    def _render_jinja(self, template_str: str, data: Dict[str, Any]) -> Optional[str]:
        try:
            env = self._get_jinja_env()
            payload = dict(data)
            payload["_res_path"] = payload.get("pluResPath", "")
            return env.from_string(template_str).render(**payload)
        except Exception as exc:
            logger.error(f"[TaJiDuo Render] Jinja2 error: {exc}")
            return None

    async def render_html(
        self,
        template_name: str,
        data: Dict[str, Any],
        options: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        content = self.get_template(template_name)
        if not content:
            logger.error(f"[TaJiDuo Render] Template not found: {template_name}")
            return None
        adapted = self._adapt_template(content)
        adapted = self._inline_assets(adapted)
        html = self._render_jinja(adapted, data)
        if not html:
            return None
        return await self._screenshot(html, template_name, options or {})

    async def _ensure_browser(self) -> None:
        from playwright.async_api import async_playwright

        if not self._playwright:
            self._playwright = await async_playwright().start()
        if not self._browser or not self._browser.is_connected():
            self._browser = await self._playwright.chromium.launch()

    async def _screenshot(
        self,
        html: str,
        template_name: str,
        options: Dict[str, Any],
    ) -> Optional[str]:
        output_path = os.path.join(
            self._output_dir, f"render_{uuid.uuid4().hex[:8]}.png"
        )
        temp_html = os.path.join(
            os.path.dirname(os.path.abspath(os.path.join(self.res_path, template_name))),
            f"tmp_{uuid.uuid4().hex[:8]}.html",
        )

        try:
            async with self._lock:
                await self._ensure_browser()

            with open(temp_html, "w", encoding="utf-8") as file:
                file.write(html)

            context = await self._browser.new_context(
                device_scale_factor=float(options.get("device_scale_factor", 2.0)),
                viewport={
                    "width": int(options.get("viewport_width", 1600)),
                    "height": int(options.get("viewport_height", 1200)),
                },
            )
            page = await context.new_page()
            await page.goto(
                f"file:///{temp_html.replace(chr(92), '/')}",
                wait_until="networkidle",
                timeout=self.render_timeout,
            )
            await page.evaluate(
                """
                Promise.all(Array.from(document.images).map(img => {
                    if (img.complete) return Promise.resolve();
                    return new Promise(resolve => {
                        img.onload = resolve;
                        img.onerror = resolve;
                    });
                }))
                """
            )
            await page.wait_for_timeout(300)
            element = await page.evaluate_handle(
                "() => document.body.firstElementChild || document.body"
            )
            box = await element.bounding_box() if element else None
            if box:
                await page.set_viewport_size(
                    {
                        "width": max(int(box["width"]) + 6, 200),
                        "height": max(int(box["height"]) + 6, 200),
                    }
                )
                await page.wait_for_timeout(100)
                await element.screenshot(path=output_path, type="png")
            else:
                await page.screenshot(path=output_path, full_page=True)
            if element:
                await element.dispose()
            await page.close()
            await context.close()
            return output_path
        except Exception as exc:
            logger.error(f"[TaJiDuo Render] Playwright error: {exc}")
            return None
        finally:
            if os.path.exists(temp_html):
                try:
                    os.remove(temp_html)
                except OSError:
                    pass

    async def close(self) -> None:
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            self._cleanup_task = None
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
