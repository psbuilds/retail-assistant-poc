# Retail Memory Assistant - Demo Guide

This guide provides exact instructions for demonstrating all seven scenarios of the Retail Memory Assistant POC.

## Prerequisites

- Cognee MCP server running at `http://127.0.0.1:8101/mcp`
- Product API running at `http://127.0.0.1:8000`
- Streamlit application at `http://127.0.0.1:8501`
- Optional: Ollama running at `http://127.0.0.1:11434` for LLM responses (deterministic fallback works without it)

Start all services:
```bash
chmod +x scripts/*.sh
scripts/start_services.sh
# or start individually:
# cognee api start --port 8102
# cognee mcp start --port 8101
# uvicorn app.api.product_api:app --host 127.0.0.1 --port 8000
# streamlit run app/ui/streamlit_app.py --port 8501
```

Verify services:
```bash
scripts/health_check.sh
```

## Scenario A: Store Customer Preferences

**Step 1**: Select customer "customer_001" in the Streamlit sidebar.

**Step 2**: Enter: `I prefer Sony headphones.`
- Expected: Assistant responds "Understood! I'll remember that you prefer Sony headphones."
- Verification: Check sidebar "Memory status" - preference should be listed

**Step 3**: Enter: `My preferred budget for headphones is ₹5,000.`
- Expected: Assistant responds "Understood! I'll remember your preferred budget of ₹5,000."
- Verification: Memory status shows both Sony preference and budget

**Step 4**: (If MCP fails) - Assistant should NOT acknowledge successful storage. The response will indicate the preference could not be saved.

## Scenario B: Recall Stored Preferences

**Step 1**: Ensure customer "customer_001" is selected with stored preferences.

**Step 2**: Enter: `What do you know about my headphone preferences?`
- Expected: Assistant returns the actual stored preference (e.g., "You prefer Sony headphones.")
- Never fabricates information not retrieved

**Step 3**: Enter: `What is my budget?`
- Expected: Assistant returns "Your preferred headphone budget is ₹5,000."

**Step 4**: Enter: `What colour headphones do I prefer?`
- Expected: Assistant returns "You haven't mentioned your preferred headphone colour yet, so I don't have that information."
- Clearly distinguishes known from unknown information.

## Scenario C: Persistent Memory After Restart

**Step 1**: Store "I prefer Sony headphones." with customer_001

**Step 2**: Stop Streamlit (Ctrl+C in the terminal)

**Step 3**: Restart Streamlit: `streamlit run app/ui/streamlit_app.py --port 8501`

**Step 4**: Select customer "customer_001" in the sidebar

**Step 5**: Enter: `Which headphone brand do I prefer?`
- Expected: Assistant returns "You prefer Sony headphones."
- Verification: Preference survived the Streamlit restart
- Persistent memory must not depend on Streamlit session state or in-memory Python variables

## Scenario D: Personalized Product Recommendation

**Step 1**: Ensure customer_001 has preference "Prefers Sony headphones." stored

**Step 2**: Enter: `Recommend wireless headphones under ₹5,000.`
- Expected: 
  - Relevant persistent preferences are retrieved when personalization is useful
  - Current explicit instructions (wireless, ₹5,000 budget) take priority
  - Sony preference is not forced into the recommendation
  - Only actual products from the catalogue are shown
  - Products display real names, prices, ratings and features
  - No invented products, prices, specifications, ratings or availability

**Step 3**: Verify recommendations are grounded in the actual product catalogue

## Scenario E: Current Instruction Overrides Memory

**Step 1**: Customer_001 has stored: `I prefer Sony headphones.`

**Step 2**: Customer_001 enters: `Recommend Bose headphones under ₹8,000.`
- Expected behaviour:
  - Current request takes precedence
  - Sony preference is NOT forced into the recommendation
  - Search for Bose products within the ₹8,000 budget
  - Old persistent preferences do not contradict explicit current instructions
  - Precedence: 1. Current explicit user instruction → 2. Relevant current conversation context → 3. Relevant persistent memory → 4. Product defaults

## Scenario F: Normal Conversation

**Step 1**: Customer enters: `What is Active Noise Cancellation?`
- Expected:
  - Normal answer: "ANC stands for Active Noise Cancellation. It uses microphones to detect and cancel out ambient noise..."
  - No Cognee MCP call made
  - No Product API call made
  - Deterministic explanation if Ollama unavailable

**Step 2**: Customer enters: `Hello how are you?`
- Expected: Polite greeting response, no service calls

## Scenario G: Customer Isolation

**Step 1**: With customer_001 selected, store: `I prefer Sony headphones.`

**Step 2**: Switch to customer_002 (or create a second customer profile)

**Step 3**: With customer_002 selected, enter: `What do you know about my headphone preferences?`
- Expected: Assistant returns "You haven't mentioned your preferred headphone brand yet, so I don't have that information."
- Verification: Customer A retrieves only their own preferences
- Verification: Customer B retrieves only their own preferences
- Verification: Switching customers does not leak memories
- Every memory operation uses the correct customer-specific dataset

## Verification Steps

After each scenario, verify:

1. **Memory status sidebar**: Shows stored preferences for the current customer
2. **Conversation history**: Contains the exchange
3. **Response mode**: Correctly shows STORE, RECALL, NORMAL_RESPONSE, or PRODUCT_DISCOVERY
4. **No errors**: Assistant handles failures gracefully (e.g., "Memory recall failed:")

## Known Behaviours

- Clearing conversation (sidebar button) preserves Cognee memories
- "Clear All Cognee Memories" deletes memories for the current customer's dataset
- MCP sessions may need restart during Streamlit reruns (adapter includes recovery logic)
- Deterministic fallback works without Ollama - recommendations use actual catalogue data