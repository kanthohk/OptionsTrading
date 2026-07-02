import requests

class AlertMobile:
    def __init__(self):
        # Define your unique topic and message
        topic = "MadhuSashi_Market_Alert"  # Use the same name you entered in the app
        self.url = f"https://ntfy.sh/{topic}"
    def send(self, heading, message, priority=5):
        # Use 'urgent' priority (5) to trigger the ring-like behavior
        headers = {
            "Title": heading,
            "Priority": f"{priority}",  # This ensures a loud notification
            "Tags": "warning,phone"  # Adds visual icons to the alert
            }
        #
        try:
            response = requests.post(self.url, data=message.encode('utf-8'), headers=headers)
            if response.status_code == 200:
                print("Successfully triggered the ring!")
                return True
            else:
                print(f"Failed with status: {response.status_code}")
                return False
        except Exception as e:
            print(f"An error occurred: {e}")
            return False
if __name__ == "__main__":
    AlertMobile().send(heading="TestHeading", message="TestTest")