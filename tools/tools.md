# Tools

## Tavily Search

This project MUST use Tavily as the ONLY external search tool.

Purpose:
- Find ACP suppliers in India and China
- Retrieve supplier information and price ranges

Usage Rules:

- ALWAYS use Tavily when searching for suppliers
- NEVER hardcode supplier data
- NEVER skip Tavily search step

Allowed:
- Supplier discovery
- Price ranges
- Company information

Not Allowed:
- Final decision making
- Raw data output without cleaning

Output Handling:

- Extract structured data from Tavily results
- Convert text into usable fields
- Remove irrelevant information