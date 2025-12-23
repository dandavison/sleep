fetch:
	uv run sleep sync && uv run sleep build

serve:
	uv run sleep serve --port 7777

.PHONY: fetch serve
