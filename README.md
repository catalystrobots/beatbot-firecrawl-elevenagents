# Beatbot AI (Firecrawl + ElevenAgents Demo)

## Overview
Beatbot is an AI-powered robotic DJ and interactive character system designed for live environments, streaming, and entertainment applications.

This demo showcases how Beatbot integrates:
- **Firecrawl Search** for real-time, structured web data retrieval
- **ElevenLabs ElevenAgents** for natural, conversational voice interaction
- A live system that connects AI decision-making to physical robot behavior

## Key Concept
Beatbot is not a chatbot — it is a **live agent with presence**.

Users speak to Beatbot in real time. The system:
1. Listens via ElevenAgents voice interface  
2. Determines if current information is needed  
3. Uses Firecrawl to retrieve relevant, structured web data  
4. Generates a grounded response  
5. Speaks and animates in sync  

## Architecture Highlights
- **Current Info Service**  
  Handles real-time queries using Firecrawl with domain controls and fallback behavior  

- **Eleven Agent Bridge**  
  Exposes controlled local tools (`lookupCurrentInfo`, `getBeatbotContext`) to the voice agent  

- **Live Conversation System**  
  Manages active listening, response timing, and session lifecycle  

- **ElevenAgents Integration**  
  Embedded conversational agent with tool access and streaming voice output  

## Why This Is Unique
This project demonstrates a step toward **physically embodied AI agents** that:
- Access live internet data
- Communicate naturally through voice
- Operate in real-world environments (not just chat interfaces)

## Notes
This repository contains a **focused subset** of the system highlighting Firecrawl and ElevenAgents integration.

Full production code, hardware control systems, and proprietary components are not included.
