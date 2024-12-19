from flask import Flask, request, jsonify, render_template
from pusher import Pusher
import threading
from azure.eventhub import EventHubConsumerClient
from collections import deque
import json
import time
import dotenv
import os
import logging
from datetime import datetime

logger = logging.getLogger("my_app_logger")
logger.setLevel(logging.INFO)
file_handler = logging.FileHandler("app.log")
file_handler.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)
logger.propagate = False

dotenv.load_dotenv()

app = Flask(__name__)

pusher_client = Pusher(
  app_id=os.getenv("PUSHER_APP_ID"),
  key=os.getenv("PUSHER_KEY"),
  secret=os.getenv("PUSHER_SECRET"),
  cluster=os.getenv("PUSHER_CLUSTER"),
  ssl=True
)

EVENT_HUB_CONNECTION_STR = os.getenv("EVENT_HUB_CONNECTION_STR")
EVENT_HUB_NAME = os.getenv("EVENT_HUB_NAME")

if not EVENT_HUB_CONNECTION_STR or not EVENT_HUB_NAME:
    raise ValueError("EVENT_HUB_CONNECTION_STR and EVENT_HUB_NAME must be set")

patient_data_store = {}

class PatientMonitor:
    def __init__(self, patient_id):
        self.patient_id = patient_id
        self.vitals = deque(maxlen=1000)
        self.monitoring_start_time = datetime.now()
        self.monitoring_active = False
        
    def add_vital(self, vital_data):
        self.vitals.append(vital_data)
    
    def stop_monitoring(self):
        self.monitoring_active = False
        self.monitoring_end_time = datetime.now()
        return {
            "monitoring_start_time": self.monitoring_start_time.isoformat(),
            "monitoring_end_time": self.monitoring_end_time.isoformat(),
            "total_vitals_recorded": len(self.vitals)
        }

@app.route("/stop_monitoring", methods=["POST"])
def stop_monitoring():
    global patient_data_store
    data = request.json
    patient_id = int(data.get("patient_id"))
    
    if not patient_id:
        return jsonify({"error": "patient_id is required"}), 400
        
    if patient_id not in patient_data_store:
        logger.error(f"Patient not found or not being monitored: {patient_data_store[patient_id]}")
        return jsonify({"error": "Patient not found or not being monitored"}), 404
    
    patient = patient_data_store[patient_id]
    monitoring_summary = patient.stop_monitoring()
    
    pusher_client.trigger(f'patient-{patient_id}', 'monitoring_stopped', {
        "patient_id": patient_id,
        "end_time": monitoring_summary["monitoring_end_time"]
    })
    
    del patient_data_store[patient_id]
    
    return jsonify({ 
        "message": f"Monitoring stopped for patient {patient_id}",
        "summary": monitoring_summary
    }), 200

@app.route("/prepare_patient", methods=["POST"])
def prepare_patient():
    global patient_data_store
    data = request.json
    patient_id = data.get("patient_id")
    
    if not patient_id:
        return jsonify({"error": "patient_id is required"}), 400
    
    patient_data_store[patient_id] = PatientMonitor(patient_id)
    
    return jsonify({
        "message": f"Monitoring initiated for patient {patient_id}",
        "monitoring_start_time": patient_data_store[patient_id].monitoring_start_time.isoformat()
    }), 200

@app.route("/notify_doctor", methods=["GET"])
def notify_doctor():
    global patient_data_store
    patient_id = int(request.args.get("patient_id"))
    
    if not patient_id or patient_id not in patient_data_store:
        return jsonify({"error": "Invalid patient_id or patient not being monitored"}), 400
    
    patient = patient_data_store[patient_id]
    
    data =  {
        "patient_id": patient_id,
        "vitals": list(patient.vitals),
        "monitoring_start_time": patient.monitoring_start_time.isoformat()
    }  
    patient.monitoring_active = True
    
    logger.info(f"Notified doctor for with data {data}")
    return render_template("index.html", data=data)

def process_event(partition_context, event):
    try:
        global patient_data_store
        data = json.loads(event.body_as_str())
        patient_id = data.get("patient_id")
        logger.info(f"Processing vital data for patient_id: {patient_id}")
        if not patient_id or patient_id not in patient_data_store:
            return
        
        patient = patient_data_store[patient_id]
        timestamp = datetime.now().isoformat()
        vital_data = {**data, "timestamp": timestamp}
        patient.add_vital(vital_data)
        data = {
            "vital_data": vital_data
        }
        if patient.monitoring_active:
            ret = pusher_client.trigger(f'patient-{patient_id}', 'patient_vitals', data=data)
            logger.info(f"Pusher response: {ret}")
        
        logger.info(f"Processed vital signs for patient {patient_id}----{data}")
        
    except Exception as e:
        logger.error(f"Error processing vital data: {str(e)}")

def start_eventhub_listener():
    while True:
        try:
            client = EventHubConsumerClient.from_connection_string(
                conn_str=EVENT_HUB_CONNECTION_STR,
                consumer_group="$Default",
                eventhub_name=EVENT_HUB_NAME
            )
            logger.info("Starting Event Hub Listener...")
            with client:
                client.receive(on_event=process_event, starting_position="-1")
        except Exception as e:
            logger.error(f"Event Hub listener error: {str(e)}")
            time.sleep(5)

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "message": "Success",
    }), 200 

# Initialize EventHub listener thread
eventhub_thread = None

def init_eventhub_listener():
    global eventhub_thread
    if not eventhub_thread or not eventhub_thread.is_alive():
        logger.info("Initializing EventHub listener thread...")
        eventhub_thread = threading.Thread(target=start_eventhub_listener, daemon=True)
        eventhub_thread.start()

# Start the EventHub listener when the app starts
init_eventhub_listener()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)