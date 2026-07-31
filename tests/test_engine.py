from app.engine.decision_engine import DecisionEngine

engine = DecisionEngine()

engine.load_workflow("internet")

print("\n=== START NODE ===")
start = engine.get_start_node()
print(start)

print("\n=== AFTER ANSWERING 'YES' ===")
next_node = engine.get_next_node("check_scope", "yes")
print(next_node)

print("\n=== AFTER ANSWERING 'WIFI' ===")
wifi_node = engine.get_next_node("check_connection", "wifi")
print(wifi_node)