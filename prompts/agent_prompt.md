# Agent Instructions

You are building an AI supplier decision system.

Follow STRICTLY:

- Always include risk scoring
- Always include value scoring
- Always return Top 10 suppliers
- Always provide explanation

Do NOT:

- Select based on price only
- Use raw Tavily data
- Skip cleaning
- Ignore risk

Goal:

Recommend the safest and most valuable supplier, not the cheapest.

## Tavily API Usage

You MUST use the Tavily API via:

services/tavily_client.py

Function:
search_suppliers(query)

Rules:

- Always call search_suppliers() when retrieving supplier data
- Do NOT generate fake suppliers
- Do NOT skip API calls