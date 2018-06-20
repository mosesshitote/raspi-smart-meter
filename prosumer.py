from time import sleep
import time
import json
from web3 import Web3, HTTPProvider, IPCProvider
import threading
import random
from Adafruit_IO import *
import addresses
from web3.middleware import geth_poa_middleware

SHUNT_OHMS = 0.1
MAX_EXPECTED_AMPS = 1.0
SEC_BTWN_READS = 5
EN_THRESHOLD = 50
MMA_N = 50
INA_SAMPLES = 10
INA_ADDRESS = 0x40
AIO = Client(addresses.AIO_ADDR)

class ProsumerMeter (threading.Thread):
    # Event alerts thread that program has been exited
    # Threadlock used to prevent simultaneous read/write to sensor data
    def __init__(self, threadID, name, threadLock, event):
        threading.Thread.__init__(self)
        self.threadID = threadID
        self.name = name
        self.tLock = threadLock
        self.event = event

        self.data = {"voltage": 0.0, "current": 0.0, "power": 0.0, "time": time.time()}
        self.local_energy_stored = 0.0

        self.mmaCurrentSum = 0.0
        self.mmaCurrent = 0.0
        self.mmaVoltageSum = 0.0
        self.mmaVoltage = 0.0
        self.mmaPowerSum = 0.0
        self.mmaPower = 0.0

        self.contract_instance = None
        self.setup_web3()
    
    # Method called when thread is started
    # Read from INA sensor and respond to blockchain events
    def run(self):
        if (self.contract_instance == None):
            print("Contract instance not initialized")
            return
        
        avail_energy = self.contract_instance.functions.getAvailableEnergy().call()
        print("PROS: Prosumer running. Available energy = {}".format(avail_energy))

        # Register user if not already registered
        isRegistered = self.contract_instance.functions.isRegistered(self.eth_account).call()
        if isRegistered != True:
            txHash = self.contract_instance.functions.registerUser().transact({"from": self.eth_account})
        else: # Fetch initial coin balance
            coin_balance = self.contract_instance.functions.getCoinBalance(self.eth_account).call()
            print("PROS: Coin balance: {}".format(coin_balance))
        
        # Preload MMAs
        #self.preload_mma()
        
        # Run until interrupted from main thread
        while not self.event.is_set():
            self.read_ina219()
            
            # Unlock account on each iteration so never gets locked out
            self.w3.personal.unlockAccount(self.eth_account, addresses.PROS_PASS)
            
            # Fetch log event entries and respond accordingly
            new_consumed_entries = self.consumed_event_filter.get_new_entries()
            for e in new_consumed_entries:
                self.handle_consumed_event(e)
                
            new_generation_entries = self.generated_event_filter.get_new_entries()
            for e in new_generation_entries:
                self.handle_generation_event(e)

            sleep(SEC_BTWN_READS)

    # If energy generated by self, update energy balance and call auctionEnd function after expiry
    def handle_generation_event(self, e):
        print("PROS: EnergyGenerated event: {}".format(e['event']))
        if (e['args']['createdBy'] == self.eth_account):
            energy_balance = self.contract_instance.functions.getEnergyBalance(self.eth_account).call()
            print("PROS: Generated receipt. Updated energy balance: {}. Auction id: {}".format(energy_balance, e['args']['auctionId']))
            
            t = threading.Timer(10.0, self.end_auction, [e['args']['auctionId']])
            t.start()

    # If consumed energy came from an auction generated by self, update coin and energy balances
    def handle_consumed_event(self, e):
        if (e['args']['createdBy'] == self.eth_account):
            coin_balance = self.contract_instance.functions.getCoinBalance(self.eth_account).call()
            energy_balance = self.contract_instance.functions.getEnergyBalance(self.eth_account).call()
            print("PROS: Energy consumed on auction {}. Updated coin balance: {}. Updated energy balance: {}".format(int(e['args']['auctionId']), coin_balance, energy_balance))

    # External-facing function, for Flask server to fetch live meter reads
    def grab_data(self):
        local_data = self.data
        return local_data
    
    # Read from INA219 sensor and send generateEnergy transactions when appropriate
    def read_ina219(self):
        self.tLock.acquire()
        try:
            v = random.randint(10,12)
            i = random.randint(1,2)
            #p = v * i
            p = 8

            self.local_energy_stored += p

            print('PROS power: {}'.format(p))
            print('Local energy: {}'.format(self.local_energy_stored))
        
            currentTime = time.time()
            self.data['time'] = currentTime
            self.data['voltage'] = v
            self.data['current'] = i
            self.data['power'] = p
            
            # Send data to Adafruit IO data store
            data = Data(value=p, created_epoch=currentTime)
            AIO.create_data('solardata', data)

            '''
            self.update_mma()
            self.local_energy_stored += SEC_BTWN_READS * self.mmaPower

            self.data['voltage'] = self.mmaVoltage
            self.data['current'] = self.mmaCurrent
            self.data['power'] = self.mmaPower
            '''
        except:
            print("PROS: Error")
            raise
        else:
            # if local energy exceeds limit, create an auction
            if (int(self.local_energy_stored) > EN_THRESHOLD):
                print("PROS: local storage exceeded")
                self.send_generate()
        finally:
            self.tLock.release()

    # Setup all web3-related functionality (web3 instance, eth account, contract instance)
    def setup_web3(self):
        # Either ganache (HTTPProvider) or local Rinkeby node (IPCProvider)
        #self.w3 = Web3(HTTPProvider('http://localhost:8545'))
        self.w3 = Web3(IPCProvider('/Users/turkg/Library/Ethereum/rinkeby/geth.ipc'))
        
        # Required for web3.py using POA chains
        self.w3.middleware_stack.inject(geth_poa_middleware, layer=0)

        self.eth_account = self.w3.eth.accounts[0]

        print("PROS: Connected to web3:{}".format(self.w3.eth.blockNumber))
        print("PROS: Eth account: {}".format(self.eth_account))

        # Initialize smart contract and set up event handlers
        with open('./EnergyMarket.json', 'r') as f:
            energy_contract = json.load(f)
            plain_address = addresses.CONTRACT_ADDR
            checksum_address = self.w3.toChecksumAddress(plain_address)
            print('checksum addr: {}'.format(checksum_address))
            self.contract_instance = self.w3.eth.contract(address=checksum_address, abi=energy_contract["abi"])
            
            #new syntax
            self.generated_event_filter = self.contract_instance.events.EnergyGenerated.createFilter(fromBlock='latest', toBlock='latest')
            self.consumed_event_filter = self.contract_instance.events.EnergyConsumed.createFilter(fromBlock='latest', toBlock='latest')

    # Start a new auction
    def send_generate(self):
        hash = self.contract_instance.functions.generateEnergy(int(self.local_energy_stored), 10).transact({"from": self.eth_account})
        self.local_energy_stored = 0
            
        # Only for ganache since Rinkeby will have a delay and so getTransactionReceipt returns None
        '''
        receipt = self.w3.eth.getTransactionReceipt(hash)
        print("PROS: Receipt: {}".format(receipt))
        
        rich_log = self.contract_instance.events.EnergyGenerated().processReceipt(receipt)[0]
        print("PROS: Event: {}\nArgs: {}".format(rich_log['event'], rich_log['args']))
        '''

    # Trigger the end of auction number auctionId
    def end_auction(self, auctionId):
        hash = self.contract_instance.functions.endAuction(auctionId).transact({'from': self.eth_account})     
        
        # Only for ganache
        '''
        receipt = self.w3.eth.getTransactionReceipt(hash)
        rich_logs = self.contract_instance.events.AuctionEnded().processReceipt(receipt)
        print("PROS: Auction ended\nEvent: {} Logs: {}".format(rich_logs[0]['event'], rich_logs[0]['args']))
        '''

    # Obtain a set of readings to initialize MMA
    def preload_mma(self):
        for i in range(MMA_N):
            self.mmaCurrentSum += self.ina.current()
            self.mmaVoltageSum += self.ina.voltage()
            self.mmaPowerSum += self.ina.power()
        
        self.mmaCurrent = mmaCurrentSum / MMA_N
        self.mmaVoltage = mmaVoltageSum / MMA_N
        self.mmaPower = mmaPowerSum / MMA_N

    # Update MMA values by collecting a new set of readings
    def update_mma(self):
        for i in range(INA_SAMPLES):
            try: 
                v = self.ina.voltage()
                i = self.ina.current()
                p = self.ina.power()
            
            except DeviceRangeError as e:
                continue
            
            else: 
                self.mmaCurrentSum -= self.mmaCurrent
                self.mmaCurrentSum += i
                self.mmaCurrent = self.mmaCurrentSum / MMA_N
                
                self.mmaVoltageSum -= self.mmaVoltage
                self.mmaVoltageSum += v
                self.mmaVoltage = self.mmaVoltageSum / MMA_N
                
                self.mmaPowerSum -= self.mmaPower
                self.mmaPowerSum += p
                self.mmaPower = self.mmaPowerSum / MMA_N
