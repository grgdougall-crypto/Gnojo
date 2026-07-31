from app.engine.decision_engine import DecisionEngine

engine = DecisionEngine()
engine.load_workflow("internet")

node = engine.get_start_node()

while node:

    print("\n--------------------------------")
    print(f"Node ID: {node['id']}")
    print(f"Type: {node['type']}")

    if node["type"] == "question":
        print(f"Question: {node['question']}")

        answer = list(node["answers"].keys())[0]
        print(f"Auto Answer: {answer}")

        node = engine.advance(node, answer)

    elif node["type"] == "instruction":
        print(f"Instruction: {node['title']}")
        print(node["instruction"])
        print("Continuing...")

        node = engine.advance(node)

    elif node["type"] == "resolution":
        print(f"Resolution: {node['title']}")
        print(node["message"])
        break